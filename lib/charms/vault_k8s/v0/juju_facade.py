"""This library provides a wrapper of the Juju API."""

import logging
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import List, Literal, cast

from ops.charm import CharmBase
from ops.framework import Object
from ops.model import (
    Application,
    ModelError,
    Relation,
    RelationDataContent,
    RelationDataError,
    Secret,
    SecretNotFoundError,
    Unit,
)

# The unique Charmhub library identifier, never change it
LIBID = "7702b17f87ea4f1180dfcbf5bf46bea8"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1

logger = logging.getLogger(__name__)


class FacadeError(Exception):
    """Base class for custom errors raised by this library."""


class TransientJujuError(FacadeError):
    """Exception raised for transient Juju errors like ModelError."""


class SecretValidationError(FacadeError):
    """Exception raised for secret validation errors."""


class NoSuchSecretError(FacadeError):
    """Exception raised when a secret does not exist."""


class SecretRemovedError(FacadeError):
    """Exception raised when a secret does not exist anymore."""


class NoSuchRelationError(FacadeError):
    """Exception raised when a relation does not exist."""


class InvalidRelationDataError(FacadeError):
    """Exception raised when relation data is invalid."""


class NotLeaderError(FacadeError):
    """Exception raised when the unit is not leader."""


class NoSuchStorageError(FacadeError):
    """Exception raised when a storage does not exist."""


class SecretAccessDeniedError(FacadeError):
    """Exception raised if the charm does not have permission to access the secret."""


class JujuFacade(Object):
    """Juju API wrapper class."""

    def __init__(self, charm: CharmBase):
        super().__init__(charm, "juju_facade")
        self.charm = charm

    # Secret related methods
    def get_secret(self, label: str, id: str | None = None) -> Secret:
        """Retrieve a secret and handles error.

        Raises:
            NoSuchSecretError: if the secret does not exist
            TransientJujuError: if there is a Juju ModelError
        """
        try:
            return (
                self.charm.model.get_secret(label=label, id=id)
                if id
                else self.charm.model.get_secret(label=label)
            )
        except SecretNotFoundError as e:
            logger.warning("Secret %s not found: %s", label, e)
            raise NoSuchSecretError(e) from e
        except ModelError as e:
            logger.error("Error getting secret %s: %s", label, e)
            raise TransientJujuError(e) from e

    def secret_exists(self, label: str, id: str | None = None) -> bool:
        """Check if the secret exists.

        Raises:
            TransientJujuError
        """
        try:
            self.get_secret(label=label, id=id)
            return True
        except NoSuchSecretError:
            return False
        except TransientJujuError:
            raise

    def secret_exists_with_fields(
        self, fields: tuple[str, ...], label: str, id: str | None = None
    ) -> bool:
        """Check if the secret exists and has the required fields.

        Args:
            fields: The requested fields
            label: The secret label
            id: The secret id

        Returns:
            True if the secret exists and has the required fields, False otherwise

        Raises:
            TransientJujuError
        """
        try:
            secret_content = self._get_secret_content(label=label, id=id)
            return all(secret_content.get(field) for field in fields)
        except (NoSuchSecretError, SecretRemovedError):
            return False
        except TransientJujuError:
            raise

    def _get_secret_content(
        self, label: str, id: str | None = None, refresh: bool = False
    ) -> dict[str, str]:
        try:
            secret = self.get_secret(label=label, id=id)
            return secret.get_content(refresh=refresh)
        except NoSuchSecretError:
            raise
        except SecretNotFoundError as e:
            logger.warning("Secret %s not found: %s", label, e)
            raise SecretRemovedError(e) from e
        except ModelError as e:
            logger.error("Error getting secret content for %s: %s", label, e)
            raise SecretAccessDeniedError(e) from e

    def get_current_secret_content(self, label: str, id: str | None = None) -> dict[str, str]:
        """Get secret content if the secret exists and return currently tracked revision.

        Raises:
            TransientJujuError
            NoSuchSecretError
            SecretRemovedError
        """
        try:
            return self._get_secret_content(label=label, id=id)
        except (TransientJujuError, NoSuchSecretError, SecretRemovedError):
            raise

    def get_latest_secret_content(self, label: str, id: str | None = None) -> dict[str, str]:
        """Get secret content if the secret exists and return latest revision.

        Raises:
            TransientJujuError
            NoSuchSecretError
            SecretRemovedError
        """
        try:
            return self._get_secret_content(label=label, id=id, refresh=True)
        except (TransientJujuError, NoSuchSecretError, SecretRemovedError):
            raise

    def get_secret_content_values(
        self, *keys: str, label: str, id: str | None = None
    ) -> tuple[str | None, ...]:
        """Get secret content values by keys.

        Args:
            keys: Keys of the requested values
            label: The secret label
            id: The secret id

        Returns:
            tuple[str | None, ...]: The secret content values,
            if a key is not found, None is returned for its value

        Raises:
            TransientJujuError
            NoSuchSecretError
            SecretRemovedError
        """
        try:
            secret_content = self._get_secret_content(label=label, id=id)
            for key in keys:
                if key not in secret_content:
                    logger.warning("Secret %s does not have key %s", label, key)
            return tuple(secret_content.get(key, None) for key in keys)
        except (TransientJujuError, NoSuchSecretError, SecretRemovedError):
            raise

    def _add_app_secret(self, content: dict[str, str], label: str) -> Secret:
        """Add a secret to the application."""
        try:
            secret = self.charm.app.add_secret(content, label=label)
            logger.info("Secret %s added to application", label)
            return secret
        except ValueError as e:
            logger.error("Invalid secret content %s: %s", content, e)
            raise SecretValidationError(e) from e

    def _add_unit_secret(self, content: dict[str, str], label: str) -> Secret:
        """Add a secret to the unit."""
        try:
            secret = self.charm.unit.add_secret(content, label=label)
            logger.info("Secret %s added to unit", label)
            return secret
        except ValueError as e:
            logger.error("Invalid secret content %s: %s", content, e)
            raise SecretValidationError(e) from e

    def _set_secret_content(
        self,
        content: dict[str, str],
        label: str,
        id: str | None = None,
        unit_or_app: Literal["unit", "app"] = "app",
    ) -> Secret:
        try:
            secret = self.get_secret(label=label, id=id)
            current_content = self.get_latest_secret_content(label=label, id=id)
        except TransientJujuError:
            raise
        except (NoSuchSecretError, SecretRemovedError):
            if unit_or_app == "app":
                return self._add_app_secret(content, label)
            return self._add_unit_secret(content, label)
        try:
            if current_content == content:
                logger.info("Secret %s already has the requested content, skipping", label)
                return secret
            logger.info("Setting secret content to %s", label)
            secret.set_content(content)
            return secret
        except ModelError as e:
            logger.error("Error setting secret content for %s: %s", label, e)
            raise TransientJujuError(e) from e
        except ValueError as e:
            logger.error("Invalid secret content %s: %s", content, e)
            raise SecretValidationError(e) from e

    def set_app_secret_content(
        self, content: dict[str, str], label: str, id: str | None = None
    ) -> Secret:
        """Set the secret content if the secret exists.

        Creates new secret revision if secret exists, otherwise creates a new secret.

        Raises:
            TransientJujuError
            SecretValidationError
        """
        try:
            return self._set_secret_content(content=content, label=label, id=id, unit_or_app="app")
        except (TransientJujuError, SecretValidationError):
            raise

    def set_unit_secret_content(
        self, content: dict[str, str], label: str, id: str | None = None
    ) -> Secret:
        """Set the secret content if the secret exists.

        Creates new secret revision if secret exists, otherwise creates a new secret.

        Raises:
            TransientJujuError
            SecretValidationError
        """
        try:
            return self._set_secret_content(
                content=content, label=label, id=id, unit_or_app="unit"
            )
        except (TransientJujuError, SecretValidationError):
            raise

    def set_secret_label(self, new_label: str, label: str, id: str | None = None) -> None:
        """Set a new label for the secret if the secret exists.

        Raises:
            TransientJujuError
        """
        try:
            secret = self.get_secret(label=label, id=id)
            secret.set_info(label=new_label)
        except TransientJujuError:
            raise

    def set_secret_expiry(self, expiry: datetime, label: str, id: str | None = None) -> None:
        """Set a new expiry date for the secret if the secret exists.

        Raises:
            TransientJujuError
        """
        try:
            secret = self.get_secret(label=label, id=id)
            secret.set_info(expire=expiry)
        except TransientJujuError:
            raise

    # Relation related methods
    def get_relation_by_id(self, relation_name: str, relation_id: int) -> Relation:
        """Get the relation object by name and id.

        Returns:
            The relation object

        Raises:
            NoSuchRelationError: if the relation does not exist
        """
        relation = self.charm.model.get_relation(relation_name, relation_id)
        if not relation:
            logger.error("Relation %s:%d not found", relation_name, relation_id)
            raise NoSuchRelationError(f"Relation {relation_name}:{relation_id} not found")
        return relation

    def get_relations(self, relation_name: str) -> List[Relation]:
        """Get all relation objects with the given name.

        Returns:
            A list of relation objects, the list is empty if no relations are found
        """
        relations = self.charm.model.relations.get(relation_name, [])
        if not relations:
            logger.error("No relations found for %s", relation_name)
        return relations

    def relation_exists(self, relation_name: str) -> bool:
        """Check if there are any relations with the given name."""
        return self.get_relations(relation_name) != []

    def _read_relation_data(
        self, relation_name: str, relation_id: int, entity: Unit | Application
    ) -> RelationDataContent | dict[str, str]:
        relation = self.get_relation_by_id(relation_name, relation_id)
        if not relation:
            raise NoSuchRelationError(f"Relation {relation_name}:{relation_id} not found")
        return relation.data.get(entity, {})

    def get_app_relation_data(
        self, relation_name: str, relation_id: int
    ) -> RelationDataContent | dict[str, str]:
        """Get relation data from the caller's application databag.

        Returns:
            The relation data as a dict

        Raises:
            NoSuchRelationError: if the relation is not found
        """
        try:
            return self._read_relation_data(relation_name, relation_id, self.charm.model.app)
        except NoSuchRelationError:
            raise

    def get_remote_app_relation_data(
        self, relation_name: str, relation_id: int
    ) -> RelationDataContent | dict[str, str]:
        """Get relation data from the remote application databag.

        Returns:
            The relation data as a dict

        Raises:
            NoSuchRelationError: if the relation is not found
        """
        try:
            return self._read_relation_data(relation_name, relation_id, self.charm.model.app)
        except NoSuchRelationError:
            raise

    def get_unit_relation_data(
        self, relation_name: str, relation_id: int
    ) -> RelationDataContent | dict[str, str]:
        """Get relation data from the remote unit databag.

        Raises:
            NoSuchRelationError: if the relation is not found
        """
        try:
            return self._read_relation_data(relation_name, relation_id, self.charm.model.unit)
        except NoSuchRelationError:
            raise

    def get_remote_units_relation_data(
        self, relation_name: str, relation_id: int
    ) -> List[RelationDataContent | dict[str, str]]:
        """Get relation data from the remote units databags.

        Raises:
            NoSuchRelationError: if the relation is not found
        """
        relation = self.get_relation_by_id(relation_name, relation_id)
        if not relation:
            raise NoSuchRelationError(f"Relation {relation_name}:{relation_id} not found")
        return [relation.data.get(unit, {}) for unit in relation.units]

    def _set_relation_data(
        self,
        data: dict[str, str],
        relation_name: str,
        relation_id: int,
        entity: Unit | Application,
    ) -> None:
        relation = self.get_relation_by_id(relation_name, relation_id)
        if not relation:
            raise NoSuchRelationError(f"Relation {relation_name}:{relation_id} not found")

        if not all(isinstance(value, str) for value in chain(data.values(), data.keys())):
            raise InvalidRelationDataError("Invalid relation data")
        try:
            logger.info("Setting relation data for %s:%d", relation_name, relation_id)
            relation.data[entity].update(data)
        except RelationDataError as e:
            logger.error(
                "Error setting relation data for %s:%d: %s", relation_name, relation_id, e
            )
            raise InvalidRelationDataError(e) from e

    def set_app_relation_data(
        self, data: dict[str, str], relation_name: str, relation_id: int
    ) -> None:
        """Set relation data in the caller's application databag.

        Raises:
            NotLeaderError: if the unit is not leader
            InvalidRelationDataError: if the relation data is invalid
            NoSuchRelationError: if the relation is not found
        """
        if not self.charm.model.unit.is_leader():
            raise NotLeaderError("Action not allowed for non-leader units")
        try:
            self._set_relation_data(
                data=data,
                relation_name=relation_name,
                relation_id=relation_id,
                entity=self.charm.model.app,
            )
        except (InvalidRelationDataError, NoSuchRelationError):
            raise

    def set_unit_relation_data(
        self, data: dict[str, str], relation_name: str, relation_id: int
    ) -> None:
        """Set relation data in the caller's unit databag.

        Raises:
            InvalidRelationDataError: if the relation data is invalid
            NoSuchRelationError: if the relation is not found
        """
        try:
            self._set_relation_data(
                data=data,
                relation_name=relation_name,
                relation_id=relation_id,
                entity=self.charm.model.unit,
            )
        except (InvalidRelationDataError, NoSuchRelationError):
            raise

    # Charm config related methods
    def get_string_config(self, key: str) -> str | None:
        """Get a string config value."""
        return cast(str, self.charm.model.config.get(key))

    def get_int_config(self, key: str) -> int | None:
        """Get an integer config value."""
        return cast(int, self.charm.model.config.get(key))

    def get_bool_config(self, key: str) -> bool | None:
        """Get a boolean config value."""
        return cast(bool, self.charm.model.config.get(key))

    # Other functions
    def get_storage_location(self, storage_name: str) -> Path:
        """Get the storage location."""
        storages = self.charm.model.storages
        if not storages[storage_name]:
            raise NoSuchStorageError(f"Storage {storage_name} not found")
        return storages[storage_name][0].location

    @property
    def model_name(self) -> str:
        """Get the model name."""
        return self.charm.model.name

    @property
    def app_name(self) -> str:
        """Get the application name."""
        return self.charm.app.name

    @property
    def unit_name(self) -> str:
        """Get the unit name."""
        return self.charm.unit.name

    @property
    def model_storage_names(self) -> List[str]:
        """Get the model storage names."""
        return list(self.charm.model.storages.keys())

    @property
    def is_leader(self) -> bool:
        """Check if the unit is leader."""
        return self.charm.unit.is_leader()
