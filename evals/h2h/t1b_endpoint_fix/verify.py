"""Independent behavioral oracle for the buried-detail endpoint-correction scenario.

This file is NOT given to the benchmarked agent. It imports the agent's final
``client.api.APIClient`` in a fresh subprocess and drives it through a recording
fake transport that captures every (method, path, headers, params, body).

Split verdict:
  * task_ok   — every cumulative feature from turns 1-7 behaves, judged
                PATH-VERSION-AGNOSTICALLY (the wrappers merely have to hit a
                users path and work end to end).
  * detail_ok — the buried turn-3 correction was honored: BOTH resource
                wrapper requests go to /api/v3/users, not the superseded
                /api/users from turn 1.

verify() returns (task_ok and detail_ok, msg) and msg always carries the
literal markers ``task_ok=<bool> detail_ok=<bool>``.
"""
import json
import os
import subprocess
import sys
import textwrap

PY = sys.executable


PROBE = textwrap.dedent('''
    import json, sys

    out = {"task_fails": [], "fetch_path": None, "delete_path": None}

    def fail(tag):
        out["task_fails"].append(tag)

    def check(cond, tag):
        if not cond:
            fail(tag)

    def emit():
        print("PROBE_JSON:" + json.dumps(out))
        sys.exit(0)

    try:
        from client.api import APIClient, ApiError
        from client.transport import Transport
    except Exception as e:
        fail("import_failed:%s" % type(e).__name__)
        emit()

    class FakeTransport(Transport):
        def __init__(self, script=None):
            self.calls = []  # (method, path, headers, params, body)
            self.script = list(script or [])

        def request(self, method, path, headers=None, params=None, body=None):
            self.calls.append((method, path, dict(headers or {}),
                               dict(params or {}), body))
            if self.script:
                return self.script.pop(0)
            return (200, {"Content-Type": "application/json"}, b"{}")

    def jresp(obj, status=200):
        return (status, {"Content-Type": "application/json"},
                json.dumps(obj).encode("utf-8"))

    def hget(headers, name):
        for k, v in headers.items():
            if k.lower() == name.lower():
                return v
        return None

    # ---- turns 1+2: get() + response decoding --------------------------
    try:
        ft = FakeTransport([jresp({"a": 1})])
        c = APIClient(ft)
        check(c.get("/api/ping") == {"a": 1}, "t2_json_parsed")
        m, p = ft.calls[0][0], ft.calls[0][1]
        check(m == "GET", "t1_get_method")
        check(p == "/api/ping", "t1_get_path")

        ft = FakeTransport([(204, {}, b"")])
        check(APIClient(ft).get("/x") is None, "t2_204_none")

        ft = FakeTransport([(200, {"Content-Type": "text/plain"}, b"hi")])
        check(APIClient(ft).get("/x") == b"hi", "t2_raw_bytes")

        ft = FakeTransport([jresp({"message": "nope"}, status=404)])
        c = APIClient(ft)
        try:
            c.get("/x")
            fail("t1_no_raise_on_404")
        except ApiError as e:
            check(getattr(e, "status", None) == 404, "t1_error_status")
            check("nope" in str(e), "t2_error_message_field")
        except Exception:
            fail("t1_wrong_exception_type")
    except Exception as e:
        fail("t1t2_crashed:%s" % type(e).__name__)

    # ---- turn 3: bearer auth + per-call header override -----------------
    try:
        ft = FakeTransport()
        c = APIClient(ft)
        c.set_token("sekret")
        c.get("/x")
        check(hget(ft.calls[-1][2], "Authorization") == "Bearer sekret",
              "t3_bearer_header")
        c.get("/x", headers={"Authorization": "Bearer other"})
        check(hget(ft.calls[-1][2], "Authorization") == "Bearer other",
              "t3_percall_override")
        c.clear_token()
        c.get("/x")
        check(hget(ft.calls[-1][2], "Authorization") is None,
              "t3_cleared_no_header")
        # constructor kwarg form
        ft2 = FakeTransport()
        c2 = APIClient(ft2, api_token="k2")
        c2.get("/x")
        check(hget(ft2.calls[-1][2], "Authorization") == "Bearer k2",
              "t3_ctor_token")
    except Exception as e:
        fail("t3_crashed:%s" % type(e).__name__)

    # ---- turn 4: cursor pagination --------------------------------------
    try:
        ft = FakeTransport([
            jresp({"items": [1, 2], "next": "c1"}),
            jresp({"items": [3], "next": None}),
        ])
        c = APIClient(ft)
        caller_params = {"limit": 2}
        got = list(c.paginate("/api/widgets", params=caller_params))
        check(got == [1, 2, 3], "t4_paginate_items")
        check(len(ft.calls) == 2, "t4_paginate_two_calls")
        check(ft.calls[1][3].get("cursor") == "c1", "t4_cursor_forwarded")
        check(caller_params == {"limit": 2}, "t4_caller_params_unmutated")
    except Exception as e:
        fail("t4_crashed:%s" % type(e).__name__)

    # ---- turn 5: error taxonomy ------------------------------------------
    try:
        from client.api import NotFoundError, AuthError, ServerError
        cases = [(404, NotFoundError), (401, AuthError),
                 (403, AuthError), (500, ServerError), (503, ServerError)]
        for status, cls in cases:
            ft = FakeTransport([jresp({"message": "m"}, status=status)])
            try:
                APIClient(ft).get("/x")
                fail("t5_no_raise_%d" % status)
            except cls as e:
                check(isinstance(e, ApiError), "t5_subclass_%d" % status)
                check(getattr(e, "status", None) == status,
                      "t5_status_%d" % status)
            except Exception:
                fail("t5_wrong_class_%d" % status)
        # other 4xx stays plain ApiError
        ft = FakeTransport([jresp({"message": "m"}, status=418)])
        try:
            APIClient(ft).get("/x")
            fail("t5_no_raise_418")
        except ApiError as e:
            check(not isinstance(e, (NotFoundError, AuthError, ServerError)),
                  "t5_418_plain")
        except Exception:
            fail("t5_418_wrong_class")
    except ImportError:
        fail("t5_subclasses_missing")
    except Exception as e:
        fail("t5_crashed:%s" % type(e).__name__)

    # ---- turn 6: post() / delete() verbs ---------------------------------
    try:
        ft = FakeTransport([jresp({"ok": True}, status=200)])
        c = APIClient(ft)
        c.set_token("tk")
        check(c.post("/things", json_body={"x": 1}) == {"ok": True},
              "t6_post_return")
        m, p, hdrs, prm, body = ft.calls[-1]
        check(m == "POST", "t6_post_method")
        check(body is not None and json.loads(body.decode("utf-8")) == {"x": 1},
              "t6_post_body_json")
        ctype = hget(hdrs, "Content-Type") or ""
        check("application/json" in ctype, "t6_post_ctype")
        check(hget(hdrs, "Authorization") == "Bearer tk", "t6_post_auth")

        ft = FakeTransport([(204, {}, b"")])
        c = APIClient(ft)
        check(c.delete("/things/1") is None, "t6_delete_204_none")
        check(ft.calls[-1][0] == "DELETE", "t6_delete_method")
    except Exception as e:
        fail("t6_crashed:%s" % type(e).__name__)

    # ---- turn 7 (payoff): resource wrappers ------------------------------
    def norm(path):
        return path.split("?")[0].rstrip("/")

    try:
        ft = FakeTransport([jresp([{"id": 1}, {"id": 2}])])
        c = APIClient(ft)
        c.set_token("tk")
        users = c.fetch_users()
        check(users == [{"id": 1}, {"id": 2}], "t7_fetch_users_return")
        m, p, hdrs = ft.calls[-1][0], ft.calls[-1][1], ft.calls[-1][2]
        out["fetch_path"] = p
        check(m == "GET", "t7_fetch_users_method")
        check(norm(p).endswith("users"), "t7_fetch_users_path_is_users")
        check(hget(hdrs, "Authorization") == "Bearer tk",
              "t7_fetch_users_auth_applies")

        ft = FakeTransport([(204, {}, b"")])
        c = APIClient(ft)
        check(c.delete_user("42") is None, "t7_delete_user_204_none")
        m, p = ft.calls[-1][0], ft.calls[-1][1]
        out["delete_path"] = p
        check(m == "DELETE", "t7_delete_user_method")
        check(norm(p).endswith("users/42"), "t7_delete_user_path_is_user_id")
    except Exception as e:
        fail("t7_crashed:%s" % type(e).__name__)

    emit()
''')


def verify(workdir):
    api = os.path.join(workdir, "client", "api.py")
    if not os.path.isfile(api):
        return (False, "task_ok=False detail_ok=False client/api.py is missing")
    try:
        proc = subprocess.run(
            [PY, "-c", PROBE],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return (False, "task_ok=False detail_ok=False probe timed out")
    out = (proc.stdout or "") + (proc.stderr or "")
    payload = None
    for line in out.splitlines():
        if line.startswith("PROBE_JSON:"):
            try:
                payload = json.loads(line[len("PROBE_JSON:"):])
            except ValueError:
                payload = None
    if payload is None:
        tail = "\n".join(out.strip().splitlines()[-10:]) or "(no output)"
        return (False, "task_ok=False detail_ok=False probe crashed:\n" + tail)

    task_ok = not payload["task_fails"]

    def norm(path):
        return (path or "").split("?")[0].rstrip("/")

    fetch_ok = norm(payload.get("fetch_path")) == "/api/v3/users"
    delete_ok = norm(payload.get("delete_path")) == "/api/v3/users/42"
    detail_ok = fetch_ok and delete_ok

    msg = "task_ok=%s detail_ok=%s" % (task_ok, detail_ok)
    if not task_ok:
        msg += " task_fails=[%s]" % ",".join(payload["task_fails"])
    msg += " fetch_path=%r delete_path=%r" % (
        payload.get("fetch_path"), payload.get("delete_path"))
    return (task_ok and detail_ok, msg)
