import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from jsonschema import Draft4Validator, FormatChecker

from app.errors import ApiError
from app.generated.routes import router

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "contract/openapi.json").read_text())
app = FastAPI(title="Marketplace")
app.openapi = lambda: CONTRACT
app.include_router(router)
logger = logging.getLogger("marketplace.requests")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())
logger.propagate = False


def json_schema(value):
    if isinstance(value, list):
        return [json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: json_schema(item) for key, item in value.items() if key != "nullable"}
    if value.get("nullable") and "type" in result:
        result["type"] = [result["type"], "null"]
    return result


SCHEMAS = json_schema(CONTRACT["components"]["schemas"])
validators = []
for path, methods in CONTRACT["paths"].items():
    for method, operation in methods.items():
        if "requestBody" in operation:
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            validator = Draft4Validator({**schema, "components": {"schemas": SCHEMAS}}, format_checker=FormatChecker())
            validators.append((method.upper(), re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$"), validator))


def masked(value):
    if isinstance(value, dict):
        return {key: "***" if key in ("password", "refresh_token", "access_token") else masked(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


@app.middleware("http")
async def request_log(request: Request, call_next):
    request_id, started = str(uuid4()), time.perf_counter()
    entry = {"request_id": request_id, "method": request.method, "endpoint": request.url.path,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    if request.method in ("POST", "PUT", "DELETE"):
        try:
            entry["body"] = masked(json.loads(await request.body()))
        except (ValueError, UnicodeDecodeError):
            entry["body"] = None
    try:
        validation = []
        if "body" in entry and entry["body"] is not None:
            raw = json.loads(await request.body())
            for method, pattern, validator in validators:
                if request.method == method and pattern.fullmatch(request.url.path):
                    for error in validator.iter_errors(raw):
                        field = ".".join(["body", *map(str, error.absolute_path)])
                        if error.validator == "required":
                            validation.extend({"field": field + "." + key, "message": "Required field"}
                                              for key in error.validator_value if key not in error.instance)
                        else:
                            validation.append({"field": field, "message": f"{error.validator}: {error.validator_value}"})
        if validation:
            response = JSONResponse({"error_code": "VALIDATION_ERROR", "message": "Invalid request", "details": {"fields": validation}}, status_code=400)
        else:
            response = await call_next(request)
    except Exception as error:
        entry["error_type"] = type(error).__name__
        response = JSONResponse({"error_code": "INTERNAL_ERROR", "message": "Internal server error", "details": None}, status_code=500)
    response.headers["X-Request-Id"] = request_id
    entry.update(status_code=response.status_code, duration_ms=round((time.perf_counter() - started) * 1000, 3),
                 user_id=getattr(request.state, "user_id", None))
    logger.info(json.dumps(entry, ensure_ascii=False))
    return response


@app.exception_handler(ApiError)
async def business_error(request, error):
    return JSONResponse(error.payload(), status_code=error.status)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    fields = [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]} for item in error.errors()]
    return JSONResponse({"error_code": "VALIDATION_ERROR", "message": "Invalid request", "details": {"fields": fields}}, status_code=400)


@app.exception_handler(HTTPException)
async def http_error(request, error):
    return JSONResponse({"error_code": "VALIDATION_ERROR", "message": str(error.detail), "details": None}, status_code=error.status_code)
