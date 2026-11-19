"""Generate DTOs and route functions from the checked-in OpenAPI contract."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contract/openapi.json"
OUTPUT = ROOT / "app/generated"


def schema_type(schema):
    if "$ref" in schema:
        return "models." + schema["$ref"].split("/")[-1]
    if schema.get("format") == "uuid":
        return "UUID"
    return {"integer": "int", "string": "str", "number": "float"}[schema["type"]]


def main():
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "__init__.py").write_text("")
    subprocess.run([
        sys.executable, "-m", "datamodel_code_generator",
        "--input", str(CONTRACT), "--input-file-type", "openapi",
        "--output", str(OUTPUT / "models.py"),
        "--output-model-type", "pydantic_v2.BaseModel",
        "--base-class", "app.serialization.ContractModel",
        "--target-python-version", "3.12", "--strict-types", "str", "int", "bool",
        "--use-subclass-enum", "--disable-timestamp",
        "--strict-nullable",
        "--output-datetime-class", "AwareDatetime",
    ], check=True)
    contract = json.loads(CONTRACT.read_text())
    lines = [
        "# Generated from contract/openapi.json; do not edit.",
        "from uuid import UUID",
        "from fastapi import APIRouter, Depends, Query",
        "from app.generated import models",
        "from app.auth import require_roles",
        "from app import service", "", "router = APIRouter()", "",
    ]
    for path, methods in contract["paths"].items():
        for method, operation in methods.items():
            args, passed = [], []
            if "requestBody" in operation:
                schema = operation["requestBody"]["content"]["application/json"]["schema"]
                args.append(f"body: {schema_type(schema)}")
                passed.append("body=body")
            for param in operation.get("parameters", []):
                name, schema = param["name"], param["schema"]
                annotation = schema_type(schema)
                if param["in"] == "path":
                    args.append(f"{name}: {annotation}")
                else:
                    default = schema.get("default")
                    if default is None:
                        annotation += " | None"
                    constraints = {"minimum": "ge", "maximum": "le", "minLength": "min_length", "maxLength": "max_length", "pattern": "pattern"}
                    options = [repr(default)] + [f"{dest}={schema[source]!r}" for source, dest in constraints.items() if source in schema]
                    args.append(f"{name}: {annotation} = Query({', '.join(options)})")
                passed.append(f"{name}={name}")
            roles = operation.get("x-roles", [])
            if roles:
                args.append(f"user=Depends(require_roles({', '.join(repr(r) for r in roles)}))")
                passed.append("user=user")
            success = next(status for status in operation["responses"] if status.startswith("2"))
            response = operation["responses"][success].get("content", {}).get("application/json", {}).get("schema")
            response_model = schema_type(response) if response else "None"
            name = operation["operationId"]
            lines += [f"@router.{method}({path!r}, status_code={success}, response_model={response_model})",
                      f"def {name}({', '.join(args)}):",
                      f"    return service.{name}({', '.join(passed)})", ""]
    (OUTPUT / "routes.py").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
