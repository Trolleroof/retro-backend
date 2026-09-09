import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

const [engineRoot, level, seedText] = process.argv.slice(2);
if (!engineRoot || !level || !seedText) throw new Error("usage: bridge ENGINE_ROOT LEVEL SEED");

const gamePath = pathToFileURL(join(engineRoot, "src/games/restaurant-arena/restaurant-game.ts")).href;
const metricsPath = pathToFileURL(join(engineRoot, "src/games/restaurant-arena/benchmark-metrics.ts")).href;
const { RestaurantGame } = await import(gamePath);
const { scoreBenchmarkEvents } = await import(metricsPath);
const game = new RestaurantGame(Number(seedText), level);
const initialState = game.state();
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity })[Symbol.asyncIterator]();
let purchaseOffered = false;
const decisionsOffered = new Set<string>();
const actions: Array<Record<string, unknown>> = [];

async function ask(kind: "decision" | "purchase", availableActions: string[]) {
  console.log(JSON.stringify({
    type: "turn",
    kind,
    observation: game.managerObservation(),
    decision: game.view().decision,
    availableActions,
  }));
  const next = await lines.next();
  if (next.done) throw new Error("runner closed stdin before episode completion");
  const command = JSON.parse(next.value);
  if (command.action !== null && !availableActions.includes(command.action)) {
    throw new Error(`invalid runner action: ${String(command.action)}`);
  }
  if (command.action !== null) {
    const result = game.step(command.action);
    actions.push({ tick: game.state().episode.tick, kind, action: command.action, result });
  }
}

while (!game.isOver()) {
  game.step("tick");
  const decision = game.view().decision;
  if (decision && !decisionsOffered.has(decision.id)) {
    decisionsOffered.add(decision.id);
    await ask("decision", decision.options.map((option: { id: string }) => `decide_${decision.id}_${option.id}`));
    continue;
  }
  const upgrade = game.view().upgrade;
  if (!purchaseOffered && !upgrade.bought && game.state().outcomes.cash >= upgrade.cost) {
    purchaseOffered = true;
    await ask("purchase", ["buy_second_stove", "pass_round"]);
  }
}

const events = game.getRecordedEvents();
console.log(JSON.stringify({
  type: "complete",
  initialState,
  finalState: game.state(),
  terminalObservation: game.managerObservation(),
  events,
  replay: game.getReplayRecord(),
  actions,
  engineScore: scoreBenchmarkEvents(events),
}));
