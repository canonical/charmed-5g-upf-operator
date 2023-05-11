# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm Library used to leverage the Multus Kubernetes CNI in charms.

- On charm installation, it will:
  - Create the requested network attachment definitions
  - Patch the statefultset with the necessary annotations for the container to have interfaces
    that use those new network attachments.
- On charm removal, it will:
  - Delete the created network attachment definitions

## Usage

```python

from kubernetes_multus import (
    KubernetesMultusCharmLib,
    NetworkAttachmentDefinition,
    NetworkAnnotation
)

class YourCharm(CharmBase):

    def __init__(self, *args):
        super().__init__(*args)
        self._kubernetes_multus = KubernetesMultusCharmLib(
            charm=self,
            network_attachment_definitions=[
                NetworkAttachmentDefinition(
                    metadata=ObjectMeta(name="access-net"),
                    spec={
                        "config": json.dumps(
                            {
                                "cniVersion": "0.3.1",
                                "type": "macvlan",
                                "ipam": {"type": "static"},
                                "capabilities": {"mac": True},
                            }
                        )
                    }
                ),
                NetworkAttachmentDefinition(
                    metadata=ObjectMeta(name="core-net"),
                    spec={
                        "config": json.dumps(
                            {
                                "cniVersion": "0.3.1",
                                "type": "macvlan",
                                "ipam": {"type": "static"},
                                "capabilities": {"mac": True},
                            }
                        )
                    }
                ),
            ],
            network_annotations=[
                NetworkAnnotation(
                    name="access-net",
                    interface="access",
                    ips=[self._access_network_ip],
                ),
                NetworkAnnotation(
                    name="core-net",
                    interface="core",
                    ips=[self._core_network_ip],
                ),
            ],
        )
```
"""

import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional, Union

import httpx
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import GenericNamespacedResource, create_namespaced_resource
from lightkube.models.core_v1 import Capabilities
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.types import PatchType
from ops.charm import CharmBase, InstallEvent, RemoveEvent, UpgradeCharmEvent
from ops.framework import Object

logger = logging.getLogger(__name__)

NetworkAttachmentDefinition = create_namespaced_resource(
    group="k8s.cni.cncf.io",
    version="v1",
    kind="NetworkAttachmentDefinition",
    plural="network-attachment-definitions",
)


@dataclass
class NetworkAnnotation:
    """NetworkAnnotation."""

    name: str
    interface: str
    ips: Optional[list] = None

    dict = asdict


class KubernetesMultusError(Exception):
    """KubernetesMultusError."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class KubernetesMultus:
    """Class containing all the Kubernetes Multus specific calls."""

    def __init__(self, namespace: str):
        self.client = Client()
        self.namespace = namespace

    def get_container_index_from_name(self, statefulset_name: str, container_name: str) -> int:
        """Returns index of container matching name.

        Args:
            statefulset_name: Statefulset name
            container_name: Container name

        Returns:
            int: Container index
        """
        statefulset = self.client.get(
            res=StatefulSet,
            name=statefulset_name,
            namespace=self.namespace,
        )
        containers = statefulset.spec.template.spec.containers  # type: ignore[attr-defined]
        for i in range(len(containers)):
            if containers[i].name == container_name:
                return i
        raise KubernetesMultusError(f"No container named {container_name} in statefulset")

    def network_attachment_definition_is_created(self, name: str) -> bool:
        """Returns whether a NetworkAttachmentDefinition is created.

        Args:
            name: NetworkAttachmentDefinition name

        Returns:
            bool: Whether the NetworkAttachmentDefinition is created
        """
        try:
            self.client.get(
                res=NetworkAttachmentDefinition,
                name=name,
                namespace=self.namespace,
            )
            logger.info(f"NetworkAttachmentDefinition {name} already created")
            return True
        except ApiError as e:
            if e.status.reason == "NotFound":
                logger.info(f"NetworkAttachmentDefinition {name} not yet created")
                return False
            else:
                raise KubernetesMultusError(
                    f"Unexpected outcome when retrieving network attachment definition {name}"
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise KubernetesMultusError(
                    "NetworkAttachmentDefinition resource not found. "
                    "You may need to install Multus CNI."
                )
            else:
                raise KubernetesMultusError(
                    f"Unexpected outcome when retrieving network attachment definition {name}"
                )

    def create_network_attachment_definition(
        self, network_attachment_definition: GenericNamespacedResource
    ) -> None:
        """Creates a NetworkAttachmentDefinition.

        Args:
            network_attachment_definition: NetworkAttachmentDefinition object
        """
        self.client.create(obj=network_attachment_definition, namespace=self.namespace)  # type: ignore[call-overload]  # noqa: E501
        logger.info(
            f"NetworkAttachmentDefinition {network_attachment_definition.metadata.name} created"  # type: ignore[union-attr]  # noqa: E501, W505
        )

    def delete_network_attachment_definition(self, name: str) -> None:
        """Deletes network attachment definition based on name.

        Args:
            name: NetworkAttachmentDefinition name
        """
        self.client.delete(res=NetworkAttachmentDefinition, name=name, namespace=self.namespace)
        logger.info(f"NetworkAttachmentDefinition {name} deleted")

    def patch_statefulset(
        self,
        name: str,
        network_annotations: list[NetworkAnnotation],
        containers_requiring_net_admin_capability: list[str],
    ) -> None:
        """Patches a statefulset with multus annotation.

        Args:
            name: Statefulset name
            network_annotations: list of network annotations
            containers_requiring_net_admin_capability: Containers requiring NET_ADMIN capability
        """
        if not network_annotations:
            logger.info("No network annotations were provided")
            return
        statefulset = self.client.get(res=StatefulSet, name=name, namespace=self.namespace)
        statefulset.spec.template.metadata.annotations["k8s.v1.cni.cncf.io/networks"] = json.dumps(  # type: ignore[attr-defined]  # noqa: E501
            [network_annotation.dict() for network_annotation in network_annotations]
        )
        for container_name in containers_requiring_net_admin_capability:
            container_index = self.get_container_index_from_name(
                statefulset_name=name, container_name=container_name
            )
            statefulset.spec.template.spec.containers[  # type: ignore[attr-defined]
                container_index
            ].securityContext.capabilities = Capabilities(
                add=[
                    "NET_ADMIN",
                ]
            )
        self.client.patch(
            res=StatefulSet,
            name=name,
            obj=statefulset,
            patch_type=PatchType.MERGE,
            namespace=self.namespace,
        )
        logger.info(f"Multus annotation added to {name} Statefulset")

    def statefulset_is_patched(
        self,
        name: str,
        network_annotations: list[NetworkAnnotation],
        containers_requiring_net_admin_capability: list[str],
    ) -> bool:
        """Returns whether the statefulset has the expected multus annotation.

        Args:
            name: Statefulset name.
            network_annotations: list of network annotations
            containers_requiring_net_admin_capability: Containers requiring NET_ADMIN capability

        Returns:
            bool: Whether the statefulset has the expected multus annotation.
        """
        statefulset = self.client.get(res=StatefulSet, name=name, namespace=self.namespace)
        if "k8s.v1.cni.cncf.io/networks" not in statefulset.spec.template.metadata.annotations:  # type: ignore[attr-defined]  # noqa: E501
            logger.info("Multus annotation not yet added to statefulset")
            return False
        if json.loads(
            statefulset.spec.template.metadata.annotations["k8s.v1.cni.cncf.io/networks"]  # type: ignore[attr-defined]  # noqa: E501
        ) != [network_annotation.dict() for network_annotation in network_annotations]:
            logger.info("Existing annotation are not identical to the expected ones")
            return False
        for container_name in containers_requiring_net_admin_capability:
            container_index = self.get_container_index_from_name(
                statefulset_name=name, container_name=container_name
            )
            if (
                "NET_ADMIN"
                not in statefulset.spec.template.spec.containers[  # type: ignore[attr-defined]
                    container_index
                ].securityContext.capabilities.add
            ):
                logger.info(
                    f"The NET_ADMIN capability is not added to the container {container_name}"
                )
                return False
        logger.info("Multus annotation already added to statefulset")
        return True


class KubernetesMultusCharmLib(Object):
    """Class to be instantiated by charms requiring Multus networking."""

    def __init__(
        self,
        charm: CharmBase,
        network_attachment_definitions: list[GenericNamespacedResource],
        network_annotations: list[NetworkAnnotation],
        containers_requiring_net_admin_capability: Optional[list[str]] = None,
    ):
        super().__init__(charm, "kubernetes-multus")

        self.network_attachment_definitions = network_attachment_definitions
        self.network_annotations = network_annotations
        if containers_requiring_net_admin_capability:
            self.containers_requiring_net_admin_capability = (
                containers_requiring_net_admin_capability
            )
        else:
            self.containers_requiring_net_admin_capability = []
        self.framework.observe(charm.on.install, self._patch)
        self.framework.observe(charm.on.upgrade_charm, self._patch)
        self.framework.observe(charm.on.remove, self._on_remove)

    def _patch(self, event: Union[InstallEvent, UpgradeCharmEvent]) -> None:
        """Creates network attachment definitions and patches statefulset.

        Args:
            event: Juju event
        """
        kubernetes_multus = KubernetesMultus(namespace=self.model.name)
        for network_attachment_definition in self.network_attachment_definitions:
            if not kubernetes_multus.network_attachment_definition_is_created(
                name=network_attachment_definition.metadata.name  # type: ignore[union-attr]  # noqa: E501
            ):
                kubernetes_multus.create_network_attachment_definition(
                    network_attachment_definition=network_attachment_definition
                )
        if not kubernetes_multus.statefulset_is_patched(
            name=self.model.app.name,
            network_annotations=self.network_annotations,
            containers_requiring_net_admin_capability=self.containers_requiring_net_admin_capability,  # noqa: E501
        ):
            kubernetes_multus.patch_statefulset(
                name=self.model.app.name,
                network_annotations=self.network_annotations,
                containers_requiring_net_admin_capability=self.containers_requiring_net_admin_capability,  # noqa: E501
            )

    def _on_remove(self, event: RemoveEvent) -> None:
        """Deletes network attachment definitions.

        Args:
            event: RemoveEvent
        """
        kubernetes_multus = KubernetesMultus(namespace=self.model.name)
        for network_attachment_definition in self.network_attachment_definitions:
            if kubernetes_multus.network_attachment_definition_is_created(
                name=network_attachment_definition.metadata.name  # type: ignore[union-attr]  # noqa: E501
            ):
                kubernetes_multus.delete_network_attachment_definition(
                    name=network_attachment_definition.metadata.name  # type: ignore[union-attr]  # noqa: E501
                )
