# Post-mortem — #38: the `oracle_*` tools and rules vanish when the `memory` toolset is disabled

## Summary

An operator running an `oracle_only` deployment (no local `MEMORY.md`, all
memory in Oracle 26ai) disables the built-in `memory` toolset to remove the
`MEMORY.md` tool. Doing so also removed every `oracle_*` tool and the memory
rules from the system prompt. The provider was still configured and healthy;
only the surface and the rules were gone.

## Timeline

1. A deployment wanted the `oracle_only` policy — `MEMORY.md` and local files
   off-limits for facts, everything in Oracle. That policy is applied by
   setting `HERMES_ORACLE_OPTIONAL_RULES=oracle_only`.
2. To actually remove the `MEMORY.md` tool from the agent, the operator turned
   off the built-in `memory` toolset for the platform.
3. After a restart, the session showed no `oracle_*` tools and no memory-rules
   block. The provider still reported ready, so the symptom read as "the
   provider loads but its tools do not appear in this process".
4. The first diagnosis was wrong: "the tools are only injected in-process and
   this session runs out of process." That did not explain the symptom, and the
   report was filed as #38 with that framing.
5. Reading the host's tool-surfacing path showed the real mechanism: memory-
   provider tools are surfaced only while the platform's toolset list contains
   `memory`. The system-prompt block is gated by the same condition. Turning
   the toolset off was doing exactly what it looked like: hiding the tools and
   the rules.

## Root cause

Hermes surfaces a memory provider's tools (and injects its system-prompt block)
only while the `memory` toolset is enabled for the platform. An `oracle_only`
deployment disables that toolset on purpose, to drop the built-in `MEMORY.md`
tool. The provider had no independent registration, so its tools and its rules
went away with the toolset — there was no code path that kept the provider's
surface alive once `memory` was off.

## Fix

The plugin now registers the eight `oracle_*` tools a second time, under its
own `oracle` toolset. Plugin toolsets resolve separately from `memory`, and a
new plugin toolset is on by default until an operator saves a list that omits
it, so the tools stay available either way. Two hooks close the remaining gaps:

- `pre_llm_call` injects the memory rules on the first turn when Hermes withheld
  the block because `memory` is off, and does nothing when the block is present.
- `pre_tool_call` enforces the `oracle_only` policy directly: it refuses the
  built-in `memory` tool and `write_file` / `patch` writes to `MEMORY.md` and
  `USER.md` while `HERMES_ORACLE_OPTIONAL_RULES` includes `oracle_only`.

Dispatch is unchanged: Hermes checks whether a tool is a memory-provider tool
before it checks the plugin registry, so with `memory` on the calls still route
through the live per-session provider; the registry copy is only reached when
no provider is active, where it returns a clean "not available" error.

## Prevention

- `tests/test_toolset.py` locks the behaviour: the toolset registration, the
  registry handler's routing (live provider vs. not-available), the availability
  check, the withheld-gate mirror, and both hooks.
- `tests/test_plugin.py` pins the hook order (`on_skill_lifecycle` first).
- `tests/test_oracle.py::test_metadata_version_consistency` pins the manifest's
  `provides_hooks` to all three hooks.
- The README and INSTALL now document "Tools and the memory toolset": why the
  `oracle_*` tools can vanish, the `hermes tools enable memory` relationship,
  and how to disable the `oracle` toolset.
- `scripts/doctor.py` reports the toolset gate and the memory-enable flag so the
  next operator who hits a blank tool surface can see the state in one command.

## Lessons

- A provider whose whole surface is gated behind one platform toolset has a
  single point of failure that looks like a runtime bug when it trips.
- A "provider loads but tools are missing" symptom should be checked against
  the tool-surfacing gate before anything in the provider's own code.
