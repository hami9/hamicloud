from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_m6_helm_chart_structure_and_security_posture() -> None:
    helm_dir = REPO_ROOT / "deploy" / "helm" / "hamicloud"
    chart_path = helm_dir / "Chart.yaml"
    values_path = helm_dir / "values.yaml"

    assert chart_path.exists(), "Missing deploy/helm/hamicloud/Chart.yaml"
    assert values_path.exists(), "Missing deploy/helm/hamicloud/values.yaml"

    chart = yaml.safe_load(chart_path.read_text(encoding="utf-8"))
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "hamicloud"
    assert chart["version"] == "1.0.0"
    assert str(chart["appVersion"]) == "1.0.0"

    values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    sha256_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    for component in ("api", "scheduler", "executor"):
        assert component in values
        img = values[component]["image"]
        assert sha256_pattern.match(img["digest"]), f"{component} image must be digest-pinned"
        resources = values[component]["resources"]
        assert "requests" in resources and "limits" in resources

    sec = values["securityContext"]
    assert sec["pod"]["runAsNonRoot"] is True
    assert sec["pod"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert sec["container"]["allowPrivilegeEscalation"] is False
    assert sec["container"]["readOnlyRootFilesystem"] is True
    assert sec["container"]["capabilities"]["drop"] == ["ALL"]

    expected_templates = [
        "api-deployment.yaml",
        "api-service.yaml",
        "scheduler-deployment.yaml",
        "executor-deployment.yaml",
        "configmap.yaml",
        "rbac.yaml",
    ]
    templates_dir = helm_dir / "templates"
    for tpl_name in expected_templates:
        tpl_path = templates_dir / tpl_name
        assert tpl_path.exists(), f"Missing Helm template: {tpl_name}"
        docs = [d for d in yaml.safe_load_all(tpl_path.read_text(encoding="utf-8")) if d is not None]
        assert len(docs) >= 1
        for doc in docs:
            if doc.get("kind") == "Deployment":
                pod_spec = doc["spec"]["template"]["spec"]
                assert pod_spec["securityContext"]["runAsNonRoot"] is True
                assert pod_spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
                for container in pod_spec["containers"]:
                    c_sec = container["securityContext"]
                    assert c_sec["allowPrivilegeEscalation"] is False
                    assert c_sec["readOnlyRootFilesystem"] is True
                    assert c_sec["capabilities"]["drop"] == ["ALL"]
                    assert "@{{ .Values." in container["image"]


def test_m6_release_documents_and_security_closure() -> None:
    release_dir = REPO_ROOT / "docs" / "release"
    required_files = [
        "v1.0.0-release-notes.md",
        "security-review-v1.0.md",
        "case-study.md",
        "six-minute-demo-guide.md",
    ]
    for fname in required_files:
        fpath = release_dir / fname
        assert fpath.exists(), f"Missing release artifact: {fname}"
        content = fpath.read_text(encoding="utf-8")
        assert len(content.strip()) > 300, f"Release artifact {fname} is unexpectedly short"

    sec_review = (release_dir / "security-review-v1.0.md").read_text(encoding="utf-8")
    assert "0` Unresolved Critical Findings" in sec_review
    assert "0` Unresolved High Findings" in sec_review
    assert "| **Critical** | `0` |" in sec_review
    assert "| **High** | `0` |" in sec_review


def test_m6_all_milestone_evidence_and_adrs_complete() -> None:
    evidence_dir = REPO_ROOT / "docs" / "evidence"
    for milestone in ("M0", "M1", "M2", "M3", "M4", "M5", "M6"):
        ev_path = evidence_dir / f"{milestone}.md"
        assert ev_path.exists(), f"Missing evidence file: {ev_path}"
        content = ev_path.read_text(encoding="utf-8")
        assert len(content.strip()) > 200
        assert "- [ ]" not in content, f"Unchecked items remain in {milestone}.md"

    adr_dir = REPO_ROOT / "docs" / "adr"
    adrs = sorted(adr_dir.glob("ADR-000*-*.md"))
    assert len(adrs) >= 5, "Expected at least 5 ADRs in docs/adr/"

    runbooks_dir = REPO_ROOT / "docs" / "runbooks"
    runbooks = list(runbooks_dir.glob("*.md"))
    assert len(runbooks) >= 5, "Expected at least 5 runbooks in docs/runbooks/"
