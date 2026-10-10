#!/usr/bin/env python3
"""Plan, apply, and verify Keycloak brute-force settings.

The default operation is read-only.  This module deliberately uses only the
Python standard library so it can be copied to a host without installing a
client package.  Credentials are read from environment variables named by the
target configuration; credential values are never read from the config file.
"""

from __future__ import annotations

import argparse
import ast
import copy
import datetime as _datetime
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


BRUTE_FORCE_KEYS = (
    "bruteForceProtected",
    "failureFactor",
    "permanentLockout",
    "maxTemporaryLockouts",
    "waitIncrementSeconds",
    "maxFailureWaitSeconds",
    "maxDeltaTime",
    "minimumQuickLoginWaitSeconds",
    "quickLoginCheckMilliSeconds",
)

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_VERIFY_FAILURE_FACTOR = 10
DEFAULT_VERIFY_CLIENT_ID = "account-console"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOCKOUT_TEXT_RE = re.compile(
    r"(?:temporar(?:ily|y)\s+disabled|too\s+many\s+(?:failed\s+)?attempts|"
    r"brute[ -]?force|account\s+locked|user\s+locked|locked\s+out)",
    re.IGNORECASE,
)


class KcError(Exception):
    """An expected configuration, transport, or Keycloak API failure."""


class ConfigError(KcError):
    """The target file is malformed or contains an unsafe value."""


class HttpResponse:
    """Small response object shared by urllib and the fake test transport."""

    def __init__(self, status: int, headers: dict[str, str] | None = None, body: bytes = b""):
        self.status = status
        self.headers = headers or {}
        self.body = body


class UrllibTransport:
    """HTTP transport.  HTTP error statuses are returned, not raised."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return HttpResponse(
                    response.status,
                    dict(response.headers.items()),
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                error.code,
                dict(error.headers.items()) if error.headers else {},
                error.read(),
            )
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", "unknown error")
            raise KcError(f"HTTP request failed for {method} {_safe_url(url)}: {reason}") from error
        except OSError as error:
            raise KcError(f"HTTP request failed for {method} {_safe_url(url)}: {error}") from error


def _safe_url(url: str) -> str:
    """Remove query strings and URL userinfo from an error message."""

    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _strip_yaml_comment(value: str) -> str:
    """Strip a YAML comment without treating # inside quotes as a comment."""

    single = False
    double = False
    escaped = False
    for index, character in enumerate(value):
        if double and escaped:
            escaped = False
            continue
        if double and character == "\\":
            escaped = True
            continue
        if character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif character == "#" and not single and not double:
            if index == 0 or value[index - 1].isspace():
                return value[:index].rstrip()
    return value.rstrip()


def _yaml_lines(text: str) -> list[tuple[int, str, int]]:
    lines: list[tuple[int, str, int]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ConfigError(f"YAML line {line_number} uses tabs for indentation")
        content = _strip_yaml_comment(raw_line).strip(" ")
        if not content or content in {"---", "..."}:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        lines.append((indent, content, line_number))
    return lines


def _split_yaml_mapping(value: str) -> tuple[str, str] | None:
    single = False
    double = False
    escaped = False
    for index, character in enumerate(value):
        if double and escaped:
            escaped = False
            continue
        if double and character == "\\":
            escaped = True
            continue
        if character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif character == ":" and not single and not double:
            if index + 1 == len(value) or value[index + 1].isspace():
                key = value[:index].strip()
                if key:
                    return key, value[index + 1 :].strip()
    return None


def _yaml_scalar(value: str) -> object:
    value = value.strip()
    if not value:
        return None
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ConfigError(f"invalid YAML double-quoted scalar: {value!r}") from error
    if value.startswith("'") and value.endswith("'"):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise ConfigError(f"invalid YAML single-quoted scalar: {value!r}") from error
    if value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            # Flow-style YAML with unquoted strings is outside the small
            # dependency-free subset; leave it as a string rather than
            # silently inventing a structure.
            pass
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value):
        try:
            return float(value)
        except ValueError:
            pass
    return value


def _parse_simple_yaml(text: str) -> object:
    """Parse the mapping/list/scalar YAML subset used by target files.

    JSON is preferred and is fully supported.  The fallback intentionally
    handles the ordinary indentation-based target format without claiming to
    be a complete YAML implementation or requiring PyYAML.
    """

    lines = _yaml_lines(text)
    if not lines:
        raise ConfigError("configuration file is empty")

    def parse_block(position: int, indent: int) -> tuple[object, int]:
        if position >= len(lines):
            return None, position
        actual_indent, first_content, line_number = lines[position]
        if actual_indent != indent:
            raise ConfigError(
                f"YAML line {line_number} has indentation {actual_indent}; expected {indent}"
            )
        is_list = first_content == "-" or first_content.startswith("- ")
        if is_list:
            result: list[object] = []
            while position < len(lines):
                current_indent, content, line_number = lines[position]
                if current_indent != indent or not (content == "-" or content.startswith("- ")):
                    break
                remainder = content[1:].strip()
                position += 1
                if not remainder:
                    if position < len(lines) and lines[position][0] > indent:
                        value, position = parse_block(position, lines[position][0])
                    else:
                        value = None
                    result.append(value)
                    continue
                mapping = _split_yaml_mapping(remainder)
                if mapping is None:
                    value = _yaml_scalar(remainder)
                    if position < len(lines) and lines[position][0] > indent:
                        raise ConfigError(
                            f"YAML line {lines[position][2]} is nested below scalar on line {line_number}"
                        )
                    result.append(value)
                    continue
                key, raw_value = mapping
                item: dict[str, object] = {}
                if raw_value:
                    item[key] = _yaml_scalar(raw_value)
                elif position < len(lines) and lines[position][0] > indent:
                    item[key], position = parse_block(position, lines[position][0])
                else:
                    item[key] = None
                if position < len(lines) and lines[position][0] > indent:
                    continuation, position = parse_block(position, lines[position][0])
                    if not isinstance(continuation, dict):
                        raise ConfigError(
                            f"YAML line {lines[position - 1][2]} must continue a mapping item"
                        )
                    item.update(continuation)
                result.append(item)
            return result, position

        result_map: dict[str, object] = {}
        while position < len(lines):
            current_indent, content, line_number = lines[position]
            if current_indent != indent:
                break
            if content == "-" or content.startswith("- "):
                raise ConfigError(f"YAML line {line_number} mixes list and mapping items")
            mapping = _split_yaml_mapping(content)
            if mapping is None:
                raise ConfigError(f"YAML line {line_number} is not a mapping")
            key, raw_value = mapping
            position += 1
            if raw_value:
                result_map[key] = _yaml_scalar(raw_value)
                if position < len(lines) and lines[position][0] > indent:
                    raise ConfigError(
                        f"YAML line {lines[position][2]} is nested below scalar on line {line_number}"
                    )
            elif position < len(lines) and lines[position][0] > indent:
                result_map[key], position = parse_block(position, lines[position][0])
            else:
                result_map[key] = None
        return result_map, position

    parsed, position = parse_block(0, lines[0][0])
    if position != len(lines):
        raise ConfigError(f"YAML line {lines[position][2]} could not be parsed")
    return parsed


def _env_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or not ENV_NAME_RE.fullmatch(value):
        raise ConfigError(f"{label} must be an environment-variable name")
    return value


def _normalize_target(raw_target: object, index: int) -> dict[str, object]:
    if not isinstance(raw_target, dict):
        raise ConfigError(f"target {index + 1} must be an object")
    target = copy.deepcopy(raw_target)
    for required in ("name", "base_url", "realm", "desired"):
        if required not in target:
            raise ConfigError(f"target {index + 1} is missing {required!r}")
    for field in ("name", "realm", "base_url"):
        if not isinstance(target[field], str) or not target[field].strip():
            raise ConfigError(f"target {index + 1} field {field!r} must be a non-empty string")

    parsed_url = urllib.parse.urlsplit(target["base_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigError(f"target {target['name']!r} base_url must be an http(s) URL")
    if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise ConfigError(f"target {target['name']!r} base_url must not contain credentials or query data")

    desired = target["desired"]
    if not isinstance(desired, dict):
        raise ConfigError(f"target {target['name']!r} desired must be an object")
    for key, value in desired.items():
        if key not in BRUTE_FORCE_KEYS:
            raise ConfigError(f"target {target['name']!r} has unsupported desired key {key!r}")
        if key == "bruteForceProtected" or key == "permanentLockout":
            if not isinstance(value, bool):
                raise ConfigError(f"desired {key} for {target['name']!r} must be boolean")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"desired {key} for {target['name']!r} must be a non-negative integer")

    has_token = "token_env" in target
    has_admin = "admin_user_env" in target
    if has_token == has_admin:
        raise ConfigError(
            f"target {target['name']!r} must contain exactly one of token_env or admin_user_env"
        )
    if has_token:
        target["token_env"] = _env_name(target["token_env"], f"target {target['name']!r} token_env")
    else:
        user_spec = target["admin_user_env"]
        password_env = target.get("admin_password_env")
        if isinstance(user_spec, dict):
            user_env = user_spec.get("username", user_spec.get("user"))
            password_env = user_spec.get("password", user_spec.get("password_env", password_env))
        else:
            user_env = user_spec
        target["admin_user_env"] = _env_name(
            user_env,
            f"target {target['name']!r} admin username environment",
        )
        target["admin_password_env"] = _env_name(
            password_env,
            f"target {target['name']!r} admin password environment",
        )

    if "verify_client_id" in target and (
        not isinstance(target["verify_client_id"], str) or not target["verify_client_id"].strip()
    ):
        raise ConfigError(f"target {target['name']!r} verify_client_id must be a string")
    return target


def validate_config(raw_config: object) -> list[dict[str, object]]:
    if isinstance(raw_config, list):
        raw_targets = raw_config
    elif isinstance(raw_config, dict):
        raw_targets = raw_config.get("targets")
    else:
        raw_targets = None
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("configuration must contain a non-empty targets list")

    targets = [_normalize_target(raw_target, index) for index, raw_target in enumerate(raw_targets)]
    names = [str(target["name"]) for target in targets]
    if len(set(names)) != len(names):
        raise ConfigError("target names must be unique")
    return targets


def load_config(path: str | os.PathLike[str]) -> list[dict[str, object]]:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read targets file {config_path}: {error}") from error
    try:
        raw_config = json.loads(text)
    except json.JSONDecodeError:
        raw_config = _parse_simple_yaml(text)
    return validate_config(raw_config)


def _read_env(name: str, target_name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise KcError(f"environment variable {name} for target {target_name!r} is not set")
    return value


def _base_url(target: dict[str, object]) -> str:
    return str(target["base_url"]).rstrip("/")


def _realm_url(target: dict[str, object]) -> str:
    return f"{_base_url(target)}/admin/realms/{urllib.parse.quote(str(target['realm']), safe='')}"


def _token_url(target: dict[str, object]) -> str:
    return f"{_base_url(target)}/realms/master/protocol/openid-connect/token"


def _user_token_url(target: dict[str, object]) -> str:
    realm = urllib.parse.quote(str(target["realm"]), safe="")
    return f"{_base_url(target)}/realms/{realm}/protocol/openid-connect/token"


def _request(
    transport: object,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> HttpResponse:
    try:
        response = transport.request(method, url, headers=headers or {}, data=data)
    except KcError:
        raise
    except Exception as error:  # pragma: no cover - defensive boundary for custom transports
        raise KcError(f"transport failed for {method} {_safe_url(url)}: {error}") from error
    if not isinstance(response, HttpResponse):
        raise KcError("transport returned an invalid response object")
    return response


def _json_body(response: HttpResponse, context: str) -> object:
    try:
        return json.loads(response.body.decode("utf-8")) if response.body else None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KcError(f"{context} returned invalid JSON (HTTP {response.status})") from error


def _api_json(
    transport: object,
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    body: object | None = None,
    expected: tuple[int, ...] = (200,),
    context: str,
) -> object:
    request_headers = dict(headers)
    request_headers.setdefault("Accept", "application/json")
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body, sort_keys=True).encode("utf-8")
    response = _request(transport, method, url, headers=request_headers, data=data)
    if response.status not in expected:
        raise KcError(f"{context} failed with HTTP {response.status}")
    if response.status == 204 or not response.body:
        return None
    return _json_body(response, context)


def _admin_headers(target: dict[str, object], transport: object) -> dict[str, str]:
    if "token_env" in target:
        token = _read_env(str(target["token_env"]), str(target["name"]))
    else:
        username = _read_env(str(target["admin_user_env"]), str(target["name"]))
        password = _read_env(str(target["admin_password_env"]), str(target["name"]))
        form = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            }
        ).encode("utf-8")
        response = _request(
            transport,
            "POST",
            _token_url(target),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=form,
        )
        if response.status != 200:
            raise KcError(f"admin-cli login for target {target['name']!r} failed with HTTP {response.status}")
        token_body = _json_body(response, "admin-cli login")
        if not isinstance(token_body, dict) or not isinstance(token_body.get("access_token"), str):
            raise KcError("admin-cli login response did not contain an access token")
        token = token_body["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _get_realm(target: dict[str, object], transport: object, headers: dict[str, str]) -> dict[str, object]:
    body = _api_json(
        transport,
        "GET",
        _realm_url(target),
        headers,
        context=f"GET realm {target['name']!r}",
    )
    if not isinstance(body, dict):
        raise KcError(f"GET realm {target['name']!r} did not return a JSON object")
    return body


def diff_brute_force(
    current: dict[str, object], desired: dict[str, object]
) -> list[tuple[str, object, object]]:
    """Return desired brute-force fields whose current values differ."""

    differences: list[tuple[str, object, object]] = []
    for key in BRUTE_FORCE_KEYS:
        if key in desired and current.get(key) != desired[key]:
            differences.append((key, current.get(key), desired[key]))
    return differences


def merge_brute_force(
    current: dict[str, object], desired: dict[str, object]
) -> dict[str, object]:
    """Merge only configured brute-force keys into the complete representation."""

    merged = copy.deepcopy(current)
    for key in BRUTE_FORCE_KEYS:
        if key in desired:
            merged[key] = copy.deepcopy(desired[key])
    return merged


def unlock_path_warnings(realm: dict[str, object]) -> list[str]:
    """Return warnings for the self-service reset path used after lockout."""

    warnings: list[str] = []
    reset_enabled = realm.get("resetPasswordAllowed") is True or realm.get("resetCredentialsAllowed") is True
    if not reset_enabled:
        warnings.append("self-service reset-credentials/reset-password flow is not enabled")
    smtp = realm.get("smtpServer")
    if not isinstance(smtp, dict) or not isinstance(smtp.get("host"), str) or not smtp["host"].strip():
        warnings.append("smtpServer.host is not configured")
    return warnings


def _display_value(value: object) -> str:
    if value is None:
        return "<missing>"
    return json.dumps(value, sort_keys=True)


def _print_diff_table(
    rows: list[tuple[str, str, str, object, object]], output: object
) -> None:
    output.write("TARGET | REALM | KEY | CURRENT | DESIRED\n")
    output.write("-------+-------+-----+---------+--------\n")
    if not rows:
        output.write("(none) | (none) | (no brute-force changes) | - | -\n")
        return
    for target_name, realm_name, key, current, desired in rows:
        output.write(
            f"{target_name} | {realm_name} | {key} | "
            f"{_display_value(current)} | {_display_value(desired)}\n"
        )


def _print_unlock_warnings(
    target: dict[str, object], warnings: list[str], output: object
) -> None:
    for warning in warnings:
        output.write(f"WARNING {target['name']} ({target['realm']}): {warning}\n")


def _target_for_selector(targets: list[dict[str, object]], selector: str) -> dict[str, object]:
    by_name = [target for target in targets if target["name"] == selector]
    if len(by_name) == 1:
        return by_name[0]
    by_realm = [target for target in targets if target["realm"] == selector]
    if len(by_realm) == 1:
        return by_realm[0]
    if len(by_realm) > 1:
        names = ", ".join(str(target["name"]) for target in by_realm)
        raise ConfigError(f"--realm {selector!r} is ambiguous; use target name ({names})")
    raise ConfigError(f"no target named or uniquely matching realm {selector!r}")


def plan_targets(targets: list[dict[str, object]], transport: object, output: object) -> int:
    rows: list[tuple[str, str, str, object, object]] = []
    changes_needed = False
    for target in targets:
        headers = _admin_headers(target, transport)
        current = _get_realm(target, transport, headers)
        _print_unlock_warnings(target, unlock_path_warnings(current), output)
        differences = diff_brute_force(current, target["desired"])
        if differences:
            changes_needed = True
        rows.extend(
            (str(target["name"]), str(target["realm"]), key, old, new)
            for key, old, new in differences
        )
    _print_diff_table(rows, output)
    if changes_needed:
        output.write("Plan requires changes; no PUT was issued.\n")
        return 2
    output.write("Plan is already satisfied; no PUT was issued.\n")
    return 0


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "keycloak-realm"


def write_backup(realm: dict[str, object], target_name: str, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = backup_dir / f"{_safe_filename(target_name)}-{stamp}.json"
    path = base
    suffix = 1
    while path.exists():
        path = backup_dir / f"{_safe_filename(target_name)}-{stamp}-{suffix}.json"
        suffix += 1
    content = (json.dumps(realm, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as backup_file:
            descriptor = -1
            backup_file.write(content)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    os.chmod(path, 0o600)
    return path


def apply_target(
    target: dict[str, object],
    transport: object,
    output: object,
    *,
    backup_dir: Path,
) -> int:
    headers = _admin_headers(target, transport)
    before = _get_realm(target, transport, headers)
    differences = diff_brute_force(before, target["desired"])
    rows = [
        (str(target["name"]), str(target["realm"]), key, old, new)
        for key, old, new in differences
    ]
    output.write("Before apply:\n")
    _print_diff_table(rows, output)
    if not differences:
        output.write("No PUT issued; the target already matches desired values.\n")
        return 0

    merged = merge_brute_force(before, target["desired"])
    for key in before:
        if key not in BRUTE_FORCE_KEYS and merged.get(key) != before.get(key):
            raise KcError(f"internal merge error changed non-brute-force key {key!r}")
    backup_path = write_backup(before, str(target["name"]), backup_dir)
    _api_json(
        transport,
        "PUT",
        _realm_url(target),
        headers,
        body=merged,
        expected=(200, 204),
        context=f"PUT realm {target['name']!r}",
    )
    after = _get_realm(target, transport, headers)
    after_differences = diff_brute_force(after, target["desired"])
    output.write(f"Backup written before PUT: {backup_path}\n")
    output.write("After apply:\n")
    after_rows = [
        (str(target["name"]), str(target["realm"]), key, after.get(key), desired)
        for key, _old, desired in differences
    ]
    _print_diff_table(after_rows, output)
    if after_differences:
        remaining = ", ".join(item[0] for item in after_differences)
        raise KcError(f"PUT succeeded but verification still differs for: {remaining}")
    output.write("PUT completed and the full realm was re-read successfully.\n")
    return 0


def _find_user(
    target: dict[str, object], transport: object, headers: dict[str, str], username: str
) -> dict[str, object]:
    query = urllib.parse.urlencode({"username": username, "exact": "true"})
    body = _api_json(
        transport,
        "GET",
        f"{_realm_url(target)}/users?{query}",
        headers,
        context=f"find throwaway user {username!r}",
    )
    if not isinstance(body, list):
        raise KcError("user lookup did not return a JSON list")
    matches = [
        user
        for user in body
        if isinstance(user, dict) and user.get("username") == username and isinstance(user.get("id"), str)
    ]
    if len(matches) != 1:
        raise KcError(f"expected exactly one throwaway user named {username!r}, found {len(matches)}")
    return matches[0]


def _response_has_lockout_text(response: HttpResponse) -> bool:
    if response.status in {423, 429}:
        return True
    if not response.body:
        return False
    try:
        parsed = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False

    def strings(value: object):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from strings(nested)

    return any(LOCKOUT_TEXT_RE.search(value) for value in strings(parsed))


def _attack_detection_locked(body: object) -> bool:
    if body is True:
        return True
    if not isinstance(body, dict):
        return False
    for key in ("disabled", "temporarilyDisabled", "locked"):
        if body.get(key) is True:
            return True
    return False


def _clear_attack_detection(
    target: dict[str, object], transport: object, headers: dict[str, str], user_id: str
) -> None:
    encoded_id = urllib.parse.quote(user_id, safe="")
    _api_json(
        transport,
        "DELETE",
        f"{_realm_url(target)}/attack-detection/brute-force/users/{encoded_id}",
        headers,
        expected=(200, 204),
        context=f"clear brute-force state for {target['name']!r}",
    )


def verify_target(
    target: dict[str, object],
    transport: object,
    output: object,
    *,
    username: str,
    attempts: int,
) -> int:
    headers = _admin_headers(target, transport)
    user = _find_user(target, transport, headers, username)
    user_id = str(user["id"])
    wrong_password = f"kc-lockout-probe-{uuid.uuid4().hex}"
    lockout_text_seen = False
    statuses: list[int] = []
    primary_error: Exception | None = None
    output.write(
        f"Running {attempts} wrong-password attempts against throwaway user {username!r}; "
        "the generated probe password is not displayed.\n"
    )
    try:
        for _attempt in range(attempts):
            form = urllib.parse.urlencode(
                {
                    "grant_type": "password",
                    "client_id": str(target.get("verify_client_id", DEFAULT_VERIFY_CLIENT_ID)),
                    "username": username,
                    "password": wrong_password,
                }
            ).encode("utf-8")
            response = _request(
                transport,
                "POST",
                _user_token_url(target),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=form,
            )
            statuses.append(response.status)
            if response.status in {200, 201}:
                raise KcError("a wrong-password probe unexpectedly received a success response")
            lockout_text_seen = lockout_text_seen or _response_has_lockout_text(response)

        status_body = _api_json(
            transport,
            "GET",
            f"{_realm_url(target)}/attack-detection/brute-force/users/{urllib.parse.quote(user_id, safe='')}",
            headers,
            context=f"read brute-force state for {target['name']!r}",
        )
        attack_locked = _attack_detection_locked(status_body)
        if not lockout_text_seen and not attack_locked:
            raise KcError(
                "the wrong-password attempts did not produce a recognizable lockout response "
                "or a locked attack-detection state"
            )
        output.write(
            f"Lockout evidence: response_text={'observed' if lockout_text_seen else 'not observed'}, "
            f"attack_detection={'locked' if attack_locked else 'not reported locked'}, "
            f"HTTP statuses={statuses}.\n"
        )
    except Exception as error:
        primary_error = error
    finally:
        try:
            _clear_attack_detection(target, transport, headers, user_id)
            output.write("Attack-detection state cleared with DELETE.\n")
        except Exception as error:
            if primary_error is None:
                primary_error = error
            else:
                primary_error = KcError(f"{primary_error}; cleanup also failed: {error}")
    if primary_error is not None:
        if isinstance(primary_error, KcError):
            raise primary_error
        raise KcError(str(primary_error)) from primary_error
    return 0


def _verify_attempt_count(target: dict[str, object], explicit: int | None) -> int:
    if explicit is not None:
        if explicit < 1:
            raise ConfigError("--attempts must be at least 1")
        return explicit
    desired = target["desired"]
    if isinstance(desired, dict) and isinstance(desired.get("failureFactor"), int):
        return int(desired["failureFactor"]) + 1
    return DEFAULT_VERIFY_FAILURE_FACTOR + 1


def _print_verify_document(target: dict[str, object], username: str, attempts: int, output: object) -> None:
    output.write("Lockout verification is documentation-only until --yes is supplied.\n")
    output.write(f"Target: {target['name']} ({target['realm']}); throwaway user: {username!r}\n")
    output.write(
        f"Procedure: submit {attempts} wrong passwords to the realm token endpoint, "
        "expect a lockout response, inspect attack-detection status, then DELETE "
        "attack-detection/brute-force/users/<id> to clear it.\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="read full realm representations and show differences")
    plan_parser.add_argument("--targets", required=True, help="YAML or JSON target file")

    apply_parser = subparsers.add_parser("apply", help="apply one target after an explicit safety acknowledgement")
    apply_parser.add_argument("--targets", required=True, help="YAML or JSON target file")
    apply_parser.add_argument("--realm", required=True, help="target name (or an unambiguous realm name)")
    apply_parser.add_argument("--yes", action="store_true", help="confirm that this one target may be changed")

    verify_parser = subparsers.add_parser("verify", help="document or run a throwaway-user lockout test")
    verify_parser.add_argument("--targets", required=True, help="YAML or JSON target file")
    verify_parser.add_argument("--realm", required=True, help="target name (or an unambiguous realm name)")
    verify_parser.add_argument("--throwaway-user", required=True, help="pre-created non-production username")
    verify_parser.add_argument("--attempts", type=int, help="wrong-password attempts; default is failureFactor + 1")
    verify_parser.add_argument("--yes", action="store_true", help="run the test and clear attack-detection state")
    verify_parser.add_argument("--client-id", help="public client with Direct Access Grants enabled")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    transport: object | None = None,
    output: object | None = None,
    error_output: object | None = None,
    backup_dir: Path | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = output or sys.stdout
    error_output = error_output or sys.stderr

    try:
        if args.command == "apply" and not args.yes:
            output.write("Refusing apply: pass --yes for the one named target.\n")
            return 2
        targets = load_config(args.targets)
        active_transport = transport or UrllibTransport()
        if args.command == "plan":
            return plan_targets(targets, active_transport, output)

        target = _target_for_selector(targets, args.realm)
        if args.command == "apply":
            return apply_target(
                target,
                active_transport,
                output,
                backup_dir=backup_dir or Path("backups"),
            )

        attempts = _verify_attempt_count(target, args.attempts)
        if args.client_id:
            target = copy.deepcopy(target)
            target["verify_client_id"] = args.client_id
        if not args.yes:
            _print_verify_document(target, args.throwaway_user, attempts, output)
            return 0
        return verify_target(
            target,
            active_transport,
            output,
            username=args.throwaway_user,
            attempts=attempts,
        )
    except (KcError, OSError, ValueError) as error:
        error_output.write(f"ERROR: {error}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
