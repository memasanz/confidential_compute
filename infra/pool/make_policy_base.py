"""OFFLINE operator tool: generate the approved worker base CCE policy once per image pair.

Requires az confcom and a Docker daemon (confcom pulls the images to hash their layers), so it runs on a
trusted workstation or build agent, never in the controller (a Container App has no Docker daemon).
Every per-worker value (IDs, hostname, client CA, MAA endpoint, TLS PEMs) is left unresolved so confcom emits
wildcards; the controller's unchanged finalizer pins them per worker. The output binds the policy to the exact
digest-pinned images and template shape; the controller refuses any mismatch.

 .venv\\Scripts\\python infra\\pool\\make_policy_base.py --worker-image REG/IMG@sha256:.. \\
     --sidecar-image REG/IMG@sha256:.. --pull-identity <id resource id> --registry REG.azurecr.io --output <file>
Run `az acr login` first. Review the policy hash/layers before baking the output into the controller image.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pool-control"))
from material import pull_configuration, template_shape, worker_template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--sidecar-image", required=True)
    parser.add_argument("--pull-identity", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--az", default="az.cmd" if sys.platform == "win32" else "az")
    args = parser.parse_args()
    images = (args.worker_image, args.sidecar_image)
    pull = pull_configuration(args.pull_identity, args.registry, "true", "acr-pull-only")
    template = worker_template(images, pull)
    shape = template_shape(template)
    with tempfile.TemporaryDirectory(dir=args.output.resolve().parent) as scratch:
        scratch = Path(scratch)
        (scratch / "template.json").write_text(json.dumps(template), encoding="utf-8")
        resolved = {"name": "base", "location": "eastus2", "subnetId": "base", "image": images[0]}
        (scratch / "parameters.json").write_text(json.dumps(
            {"parameters": {k: {"value": v} for k, v in resolved.items()}}), encoding="utf-8")
        subprocess.run([args.az, "confcom", "acipolicygen", "--template-file", str(scratch / "template.json"),
                        "--parameters", str(scratch / "parameters.json"), "--approve-wildcards"],
                       check=True, stdout=subprocess.DEVNULL)
        generated = json.loads((scratch / "template.json").read_text(encoding="utf-8-sig"))
    policy = generated["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"]
    base64.b64decode(policy, validate=True)
    if template_shape(generated) != shape:
        raise RuntimeError("confcom changed more than the policy; refusing the output")
    args.output.write_text(json.dumps({"images": list(images), "shape": shape, "ccePolicy": policy}),
                           encoding="utf-8")
    print("base policy sha256 (not a worker hostdata value):",
          hashlib.sha256(base64.b64decode(policy)).hexdigest())


if __name__ == "__main__":
    main()
