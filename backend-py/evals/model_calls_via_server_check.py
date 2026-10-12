r"""Model calls through SquirrelWisdom's ai:resolveAgenticStep (small_model_engine:
MODEL_CALLS_VIA_SQUIRRELWISDOM, _SquirrelWisdomStepAdapter), end to end without the server.

sw_api._post_json is replaced by a stand-in that does what the server command does: runs
Camerlengo's AI.resolve_agentic_step with the options from the request and returns its
dict over a JSON round trip. Everything on the client side is the real code with the
switch on: the one-shot prompt, the tool loop, web search, the session, the error paths.

The stand-in's resolve_agentic_step is whatever CAROLINE_CAMERLENGO_PATH holds. With the
copy an install ships today it takes no options, so those are dropped and the checks that
need them are reported as SKIP; with the server patch applied to that file (reasoning,
transforms, plugins, max_tokens, temperature in; usage and annotations out) all ten run.

  set CAROLINE_APP=<...>\backend-py
  set CAROLINE_CAMERLENGO_PATH=<a camerlengo directory>
  %LOCALAPPDATA%\Caroline\runtime\python\python.exe model_calls_via_server_check.py
Real model calls, under a cent."""
import asyncio, inspect, json, os, sys, time

APP = os.environ["CAROLINE_APP"]
sys.path.insert(0, APP)
os.chdir(APP)
import app.small_model_engine as e  # noqa: E402
import app.login_api as login_api  # noqa: E402

requests_seen = []
MODE = {"fail": None}


async def main():
    key = await e.get_model_provider_key()          # the stand-in server's own key, as on the real server
    server_ai = e.camerlengo_ai.AI(adapter=e.camerlengo_ai.OpenRouterAdapter(api_key=key))
    accepted = set(inspect.signature(server_ai.resolve_agentic_step).parameters)
    patched = {"reasoning", "transforms", "plugins"} <= accepted
    print("the stand-in's server function takes the options:", patched)

    async def fake_post_json(body, timeout=30.0):
        assert body["command"] == "ai:resolveAgenticStep" and body["key"] and body["session"], body.keys()
        requests_seen.append({k: v for k, v in body.items() if k not in ("messages", "tools", "key")} | {"tools": len(body.get("tools") or [])})
        if MODE["fail"]:
            return dict(MODE["fail"])
        options = {k: body[k] for k in ("reasoning", "transforms", "plugins", "max_tokens", "temperature") if k in body and k in accepted}
        wire = json.loads(json.dumps(body))       # what really crosses the network
        result = await asyncio.to_thread(lambda: server_ai.resolve_agentic_step(wire["messages"], wire.get("tools"), model=wire["model"], **options))
        return json.loads(json.dumps(dict(result, **{".status": "ok"})))

    sessions = {"n": 0}

    async def fake_session():
        sessions["n"] += 1
        return f"session-{sessions['n']}"

    e._post_json = fake_post_json
    login_api.get_v2_session = fake_session
    e.MODEL_CALLS_VIA_SQUIRRELWISDOM = True
    e.get_model_provider_key = None                 # with the switch on, nothing may ask for the key
    results = []

    def check(label, ok, detail="", needs_patch=False):
        if needs_patch and not patched:
            print("SKIP " + label + " | needs the server function with the options")
            return
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + label + (" | " + detail if detail else ""))

    # 1. A plain one-shot prompt (narrator, gate, recognition check all go this way).
    t0 = time.monotonic()
    text = await e.resolve_narration_prompt("Reply with exactly one word: OK", model=e.NARRATOR_FIRST_MODEL)
    check("one-shot prompt", text.strip() == "OK", f"{text!r} in {time.monotonic() - t0:.2f}s")

    # 2. The agentic loop with a tool run on this side, reasoning and transforms passed, usage reported.
    ai = await e._model_ai()
    usage_log, tool_runs = [], []
    tool = [{"type": "function", "function": {"name": "get_current_time", "description": "Returns the current local date and time.",
                                             "parameters": {"type": "object", "properties": {}}}}]

    def executor(name, args):
        tool_runs.append(name)
        return "2026-10-12 11:42 (Monday)"

    out = await asyncio.to_thread(lambda: ai.resolve_agentic(
        [{"role": "user", "content": "What time is it right now? Use the tool, then answer in one short sentence."}], tool, executor,
        model="openai/gpt-5-nano", max_iterations=4, transforms=e.DISABLE_MIDDLE_OUT, reasoning={"effort": "minimal"}, on_usage=usage_log.append))
    step = [r for r in requests_seen if r["tools"]][0]
    check("tool loop", tool_runs == ["get_current_time"] and "11:42" in out, f"{out!r}")
    check("options sent", step.get("reasoning") == {"effort": "minimal"} and step.get("transforms") == e.DISABLE_MIDDLE_OUT,
          str({k: step.get(k) for k in ("reasoning", "transforms", "temperature")}))
    check("usage reported", len(usage_log) >= 2 and all(u.get("prompt_tokens") for u in usage_log), str(usage_log[:1]), needs_patch=True)

    # 3. Web search: the plugin goes through the same command, sources come back as annotations.
    found = await asyncio.to_thread(e._run_web_search_tool, "web_search", {"question": "Kyiv weather forecast tomorrow"}, ai, e.camerlengo_ai.Config)
    check("web search", found.startswith("Web search results for:") and "1. " in found and "http" in found,
          found[:160].replace("\n", " / "), needs_patch=True)
    check("plugins sent", any(r.get("plugins") for r in requests_seen))

    # 4. One session serves the calls; a refused session is replaced once.
    check("session reused", sessions["n"] == 1, f"logins so far: {sessions['n']}")
    MODE["fail"] = {".status": "error", ".errcode": "202", ".reason": "Unknown or expired session"}
    refused = await asyncio.to_thread(lambda: ai.resolve_agentic([{"role": "user", "content": "hi"}], None, None, model=e.NARRATOR_FIRST_MODEL, max_iterations=1))
    check("expired session: one new login, then the error as text", sessions["n"] == 2 and refused.startswith("Agent error") and "202" in refused, refused[:120])

    # 5. The ACL refusal as it is today, and an empty wallet.
    MODE["fail"] = {".status": "error", ".errcode": "997", ".reason": "Key scope/resource ACL does not permit this call"}
    acl = await asyncio.to_thread(lambda: ai.resolve_agentic([{"role": "user", "content": "hi"}], None, None, model=e.NARRATOR_FIRST_MODEL, max_iterations=1))
    check("ACL refusal surfaces as an agent error", acl.startswith("Agent error") and "997" in acl, acl[:140])
    MODE["fail"] = {".status": "error", ".errcode": "402", ".reason": "SquirrelWisdom wallet balance is too low"}
    funds = await asyncio.to_thread(lambda: ai.resolve_agentic([{"role": "user", "content": "hi"}], None, None, model=e.NARRATOR_FIRST_MODEL, max_iterations=1))
    check("empty wallet is recognized as funds exhaustion", "insufficient funds" in funds.lower(), funds[:120])
    print(f"{sum(results)}/{len(results)} passed; requests to the command: {len(requests_seen)}")


asyncio.run(main())
