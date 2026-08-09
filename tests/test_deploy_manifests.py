"""
Validates the deploy/ infrastructure-as-code artifacts (Dockerfile, docker
compose, Kubernetes manifests) for syntactic/structural correctness.

IMPORTANT SCOPE NOTE: these tests confirm the YAML parses and has the
expected required fields/structure -- they do NOT verify the manifests
against a live Kubernetes cluster or Docker daemon (neither was available
in this build environment). Before relying on these in production, run:

    docker compose -f deploy/docker-compose.yml config
    kubectl apply --dry-run=server -f deploy/k8s/

against your own environment.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DEPLOY_DIR = Path(__file__).parent.parent / "deploy"


def _load_yaml_docs(path: Path) -> list[dict]:
    with open(path) as f:
        return [doc for doc in yaml.safe_load_all(f) if doc is not None]


def test_deploy_directory_exists():
    assert DEPLOY_DIR.is_dir()


def test_dockerfile_exists_and_has_required_stages():
    dockerfile = DEPLOY_DIR / "Dockerfile"
    assert dockerfile.exists()
    text = dockerfile.read_text()
    assert "FROM python" in text
    assert "AS builder" in text
    assert "AS runtime" in text
    assert "ENTRYPOINT" in text


def test_docker_compose_is_valid_yaml_with_required_services():
    docs = _load_yaml_docs(DEPLOY_DIR / "docker-compose.yml")
    assert len(docs) == 1
    compose = docs[0]
    assert "services" in compose
    assert "redis" in compose["services"]
    assert "worker" in compose["services"]
    assert compose["services"]["redis"]["image"].startswith("redis:")


def test_compose_worker_depends_on_redis_healthcheck():
    docs = _load_yaml_docs(DEPLOY_DIR / "docker-compose.yml")
    worker = docs[0]["services"]["worker"]
    assert worker["depends_on"]["redis"]["condition"] == "service_healthy"


def test_k8s_deployment_is_valid_yaml_with_required_fields():
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "deployment.yaml")
    assert len(docs) == 1
    deployment = docs[0]
    assert deployment["kind"] == "Deployment"
    assert deployment["apiVersion"] == "apps/v1"
    assert deployment["spec"]["replicas"] >= 1
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    assert containers[0]["name"] == "bitscrape"


def test_k8s_deployment_has_resource_limits():
    """Every production workload should declare resource requests/limits --
    a common and important omission to catch."""
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "deployment.yaml")
    container = docs[0]["spec"]["template"]["spec"]["containers"][0]
    assert "resources" in container
    assert "requests" in container["resources"]
    assert "limits" in container["resources"]


def test_k8s_deployment_has_health_probes():
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "deployment.yaml")
    container = docs[0]["spec"]["template"]["spec"]["containers"][0]
    assert "readinessProbe" in container
    assert "livenessProbe" in container


def test_k8s_deployment_spreads_across_zones():
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "deployment.yaml")
    spec = docs[0]["spec"]["template"]["spec"]
    assert "topologySpreadConstraints" in spec
    assert spec["topologySpreadConstraints"][0]["topologyKey"] == "topology.kubernetes.io/zone"


def test_k8s_hpa_is_valid_yaml_with_required_fields():
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "hpa.yaml")
    assert len(docs) == 1
    hpa = docs[0]
    assert hpa["kind"] == "HorizontalPodAutoscaler"
    assert hpa["spec"]["scaleTargetRef"]["name"] == "bitscrape-worker"
    assert hpa["spec"]["minReplicas"] < hpa["spec"]["maxReplicas"]


def test_k8s_hpa_has_scale_down_stabilization():
    """Prevents flapping -- an easy-to-forget but important HPA setting."""
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "hpa.yaml")
    behavior = docs[0]["spec"]["behavior"]
    assert behavior["scaleDown"]["stabilizationWindowSeconds"] > 0


def test_k8s_service_configmap_and_pdb_all_present():
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "service.yaml")
    kinds = {doc["kind"] for doc in docs}
    assert kinds == {"Service", "ConfigMap", "PodDisruptionBudget"}


def test_k8s_pdb_ensures_minimum_availability():
    docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "service.yaml")
    pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")
    assert pdb["spec"]["minAvailable"] >= 1


def test_all_k8s_resources_share_consistent_app_label():
    """Sanity check that Service/ConfigMap selectors will actually match
    the Deployment's pod labels -- a very common real-world misconfiguration."""
    deployment_docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "deployment.yaml")
    service_docs = _load_yaml_docs(DEPLOY_DIR / "k8s" / "service.yaml")

    pod_labels = deployment_docs[0]["spec"]["template"]["metadata"]["labels"]
    service = next(d for d in service_docs if d["kind"] == "Service")
    pdb = next(d for d in service_docs if d["kind"] == "PodDisruptionBudget")

    assert service["spec"]["selector"].items() <= pod_labels.items()
    assert pdb["spec"]["selector"]["matchLabels"].items() <= pod_labels.items()
