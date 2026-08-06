# Right now: read the run and fix what it says

You have a harness report. Fix from it, not from a theory about what the model is like.

## Read the fault classification first

The report sorts every failure into three kinds and they have completely different answers:

| | what it means | what to do |
|---|---|---|
| **config** | it was never given the means — a missing tool, a rules file contradicting the tool list | **Fix this. It is your bug.** |
| **model** | it had the tool and the instruction and did otherwise | Sharpen the rule, or accept it as the model's ceiling |
| **harness** | the scenario or the runner, not the agent | Fix the scenario. Do not touch the agent |

**Configuration faults must reach zero.** Everything else is a judgement call; that one is
not. A rules file telling a step to quote a price it has no tool to look up is a
contradiction you wrote, and no amount of rewording fixes it.

**A harness fault means the test is wrong.** Resist fixing the agent to satisfy a bad
test. A scenario that forbids the word "refund" fails a step for correctly saying "I can't
promise a refund", and changing the agent to avoid the word makes it worse.

## Then the node ranking and the timings

The report says which node holds most of the trouble, and how long each takes. A node
whose *mean* is high is composing something long every time — usually because it is
hedging around something it has no tool to answer.

## How to fix

**Change everything you are going to change, then run once.** Fixing one thing per run
means one fix per several minutes, and the failures move around between runs anyway.

**One run is not evidence.** Judgements come from `--repeat 4`. A verdict from a single
run has been wrong here, and a whole day's failure list was thrown away because of it.

**Use node scenarios to iterate.** They cost seconds; the full suite costs minutes. Run
the full suite for the verdict, not for the loop.

## When to stop and ask

Stop and put a question to the person when the fix requires a decision that is theirs:
a price, a policy, whether to refuse a kind of job, what to say when something is
declined. Do not invent a business rule to make a test pass. Say what you need and wait.
