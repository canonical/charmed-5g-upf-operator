# SD-Core UPF Operator (k8s)
[![CharmHub Badge](https://charmhub.io/sdcore-upf-k8s/badge.svg)](https://charmhub.io/sdcore-upf-k8s)

Charmed Operator for SD-Core's User Plane Function (UPF) for K8s. For more information, read [here](https://github.com/omec-project/upf).

## Pre-requisites

A Kubernetes host with a CPU supporting AVX2 and RDRAND instructions (Intel Haswell, AMD Excavator or equivalent)

A Kubernetes cluster with the Multus addon enabled.

## Usage

Enable the Multus addon on MicroK8s

```bash
sudo microk8s addons repo add community https://github.com/canonical/microk8s-community-addons --reference feat/strict-fix-multus
sudo microk8s enable multus
```

Create a Juju model:

```bash
juju add-model user-plane
```

Deploy the UPF:

```bash
juju deploy sdcore-upf-k8s --trust --channel=1.6/edge
```

### Exposing the UPF externally

If a load balancer such as `metallb` is present, the charm will configure an externally accessible
service port with the load balancer upon install of the charm.

## Image

- **bessd**: ghcr.io/canonical/sdcore-upf-bess:2.0.0
- **routectl**: ghcr.io/canonical/sdcore-upf-bess:2.0.0
- **pfcp-agent**: ghcr.io/canonical/sdcore-upf-pfcpiface:2.0.0
