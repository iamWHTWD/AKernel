"""HTTP and Kubernetes transports; neither retries ambiguous mutations."""

from __future__ import annotations

import errno
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from .errors import Blocked, Conflict, UnknownOutcome, UpgradeError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    _READ_ATTEMPTS = 4
    _RETRY_STATUS = frozenset((408, 429, 502, 503, 504))

    def __init__(self, management_url: str, resources_url: str,
                 token_path: str, ca_file: str | None = None, timeout: int = 30,
                 operation_timeout: int = 660):
        if timeout <= 0 or operation_timeout <= 0:
            raise Blocked("HTTP timeouts must be positive")
        for value, https in [(management_url, False), (resources_url, True)]:
            url = urllib.parse.urlsplit(value)
            if (url.scheme not in (["https"] if https else ["http", "https"])
                    or not url.netloc or url.username or url.password
                    or url.query or url.fragment):
                raise Blocked("invalid management/resources endpoint")
        self.management_url = management_url.rstrip("/")
        self.resources_url = resources_url.rstrip("/")
        self.token_path = token_path
        self.timeout = timeout
        self.operation_timeout = operation_timeout
        context = ssl.create_default_context(cafile=ca_file)
        self.opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def request(self, method: str, path: str, payload=None, *, public=False):
        headers = {"Accept": "application/json"}
        base = self.resources_url if public else self.management_url
        # Internal management deliberately has no public bearer token. Its
        # dedicated listener is reachable only through the operator policy.
        if public:
            token = Path(self.token_path).read_text().strip()
            if not token or "\n" in token or "\r" in token:
                raise Blocked("invalid public API token file")
            headers["Authorization"] = "Bearer " + token
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, body, headers, method=method)
        try:
            content = (self._read(request) if method == "GET" else
                       self._read_once(request, self.operation_timeout))
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("response too large")
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("object response required")
            return value
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 409:
                raise Conflict("HTTP version/identity conflict") from error
            if error.code in (400, 401, 403, 404, 405, 422):
                raise UpgradeError(f"HTTP request rejected ({error.code})") from error
            if method != "GET":
                raise UnknownOutcome(f"HTTP mutation result unknown ({error.code})") from error
            raise UpgradeError(f"HTTP read failed ({error.code})") from error
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
            if method != "GET":
                raise UnknownOutcome("HTTP mutation response unavailable or invalid") from error
            raise UpgradeError("HTTP read unavailable or invalid") from error

    def _read_once(self, request, timeout):
        with self.opener.open(request, timeout=timeout) as response:
            return response.read(8 * 1024 * 1024 + 1)

    def _read(self, request):
        # A transient read must not multiply the original timeout. Successful
        # bodies are validated by request(); malformed JSON is not retried.
        deadline = time.monotonic() + self.timeout
        timeout = self.timeout
        for attempt in range(self._READ_ATTEMPTS):
            try:
                return self._read_once(request, timeout)
            except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
                retry_after = None
                if isinstance(error, urllib.error.HTTPError):
                    retryable = error.code in self._RETRY_STATUS
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    error.close()
                else:
                    reason = error.reason if isinstance(error, urllib.error.URLError) else error
                    retryable = (not isinstance(reason, ssl.SSLError) and (
                        isinstance(reason, (TimeoutError, ConnectionError, http.client.IncompleteRead))
                        or isinstance(reason, OSError) and reason.errno in (
                            errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH,
                            errno.ETIMEDOUT, errno.ECONNRESET, errno.EPIPE, socket.EAI_AGAIN)))
                delay = max(0.5 * 2 ** attempt, self._retry_after(retry_after))
                if (not retryable or attempt + 1 == self._READ_ATTEMPTS
                        or delay >= deadline - time.monotonic()):
                    raise
                time.sleep(delay)
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise

    @staticmethod
    def _retry_after(value):
        if not value:
            return 0
        try:
            value = value.strip()
            if value.isascii() and value.isdecimal():
                # Huge valid delays become infinity and exceed the budget;
                # they must not fall back to an earlier retry.
                return float(value)
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(0, date.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
        return 0


class Inventory:
    """Existing management inventory and separately authenticated public resources."""
    def __init__(self, http: HTTP, page_size: int = 100):
        self.http, self.page_size = http, page_size

    def instances(self, node_id: str | None = None) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()
        basis = None
        page = 1
        while page <= 10000:
            query = {"page": page, "page_size": self.page_size}
            if node_id:
                query["node_id"] = node_id
            value = self.http.request("GET", "/instance-manager/query-tenant-instances?"
                                      + urllib.parse.urlencode(query))
            count, items = value.get("count"), value.get("instanceInfos")
            stamp = (value.get("epoch"), value.get("revision"), count)
            if (type(count) is not int or not 0 <= count <= 100000 or not isinstance(items, list)
                    or type(stamp[0]) is not int or stamp[0] <= 0
                    or type(stamp[1]) is not int):
                raise UpgradeError("invalid instance inventory envelope")
            if basis is None:
                basis = stamp
            elif stamp != basis:
                raise Conflict("instance inventory changed during pagination")
            for item in items:
                identity = item.get("instanceID")
                if (not identity or identity in seen or not item.get("node_id")
                        or not item.get("status")
                        or (node_id and item["node_id"] != node_id)):
                    raise UpgradeError("invalid/duplicate instance inventory")
                seen.add(identity)
                result.append(item)
            if len(result) == count:
                return result
            if not items or len(result) > count:
                raise UpgradeError("incomplete instance inventory")
            page += 1
        raise UpgradeError("instance inventory page limit exceeded")

    def resources(self) -> dict[str, dict]:
        value = self.http.request("GET", "/api/environment/v1/resources", public=True)
        items = value.get("items")
        if not isinstance(items, list):
            raise UpgradeError("missing resource inventory")
        result = {}
        for item in items:
            identity = item.get("id")
            if not identity or identity in result or item.get("status") not in (0, 1):
                raise UpgradeError("invalid/duplicate resource identity")
            for field in ("capacity", "allocatable"):
                resources = item.get(field)
                if not isinstance(resources, dict):
                    raise UpgradeError("missing node resource accounting")
                if any(type(resources.get(key)) is not int or resources[key] < 0
                       for key in ("CPU", "Memory")):
                    raise UpgradeError("invalid scalar resource accounting")
                if "Disk" in resources and (type(resources["Disk"]) is not int or resources["Disk"] < 0):
                    raise UpgradeError("invalid storage resource accounting")
                if any(key not in ("CPU", "Memory", "Disk") and resources[key]
                       for key in resources):
                    raise Blocked("device/unknown resource requires explicit upgrade qualification")
            if any(item["allocatable"].get(key, 0) > item["capacity"].get(key, 0)
                   for key in ("CPU", "Memory", "Disk")):
                raise UpgradeError("free resource exceeds capacity")
            result[identity] = item
        return result


class Kubernetes:
    """Use official client auth loading, JSON Patch tests, and DELETE preconditions."""
    def __init__(self, namespace: str, kubeconfig: str | None = None):
        try:
            from kubernetes import client, config
        except ImportError as error:
            raise Blocked("install deploy/upgrade/requirements.txt") from error
        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        self.api = client.ApiClient()
        self.namespace = namespace

    def call(self, method: str, path: str, body=None, *, patch=False, query=None):
        from kubernetes.client.exceptions import ApiException
        from urllib3.exceptions import HTTPError as TransportError
        try:
            result = self.api.call_api(
                path, method, body=body, query_params=list((query or {}).items()),
                response_type="object", auth_settings=["BearerToken"],
                header_params={"Content-Type": "application/json-patch+json"
                               if patch else "application/json"},
                _return_http_data_only=True, _request_timeout=(10, 30))
            if not isinstance(result, dict):
                raise ValueError("Kubernetes object response required")
            return result
        except ApiException as error:
            if error.status in (409, 422):
                raise Conflict("Kubernetes UID/resourceVersion conflict") from error
            if error.status in (400, 401, 403, 404, 405):
                raise UpgradeError(f"Kubernetes request rejected ({error.status})") from error
            if method != "GET":
                raise UnknownOutcome("Kubernetes mutation outcome unknown") from error
            raise UpgradeError("Kubernetes read failed") from error
        except (OSError, TransportError, ValueError) as error:
            if method != "GET":
                raise UnknownOutcome("Kubernetes mutation transport unknown") from error
            raise UpgradeError("Kubernetes read transport failed") from error

    def path(self, kind: str, name: str = "") -> str:
        prefix = "/apis/apps/v1" if kind == "daemonsets" else "/api/v1"
        return f"{prefix}/namespaces/{self.namespace}/{kind}" + ("/" + name if name else "")

    def get(self, kind: str, name: str):
        return self.call("GET", self.path(kind, name))

    def get_node(self, name: str):
        return self.call("GET", "/api/v1/nodes/" + urllib.parse.quote(name, safe=""))

    def list(self, kind: str, owner_uid: str | None = None):
        result, token = [], ""
        while True:
            query = {"limit": "100"}
            if token:
                query["continue"] = token
            value = self.call("GET", self.path(kind), query=query)
            for item in value.get("items", []):
                if owner_uid is None or any(owner.get("uid") == owner_uid
                                            for owner in item["metadata"].get("ownerReferences", [])):
                    result.append(item)
            token = value.get("metadata", {}).get("continue", "")
            if not token:
                return result

    def patch(self, kind: str, obj: dict, operations: list[dict]):
        meta = obj["metadata"]
        guards = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                  {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
        return self.call("PATCH", self.path(kind, meta["name"]), guards + operations, patch=True)

    def delete_pod(self, obj: dict):
        meta = obj["metadata"]
        return self.call("DELETE", self.path("pods", meta["name"]), {
            "apiVersion": "v1", "kind": "DeleteOptions",
            "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}})

    def create_configmap(self, name: str, data: dict[str, str]):
        return self.call("POST", self.path("configmaps"), {
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {
                "name": name, "namespace": self.namespace,
                "labels": {"akernel.io/node-upgrade": "true"}}, "data": data})

    def save_state(self, obj: dict, state: dict):
        return self.patch("configmaps", obj, [{"op": "replace", "path": "/data/state.json",
                                               "value": json.dumps(state, sort_keys=True)}])
