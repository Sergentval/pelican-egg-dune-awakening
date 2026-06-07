// Market tab: the market-bot economy writer.
//
//   7b-2 (seed/clear): post NPC sell orders from the priced+masked catalog and
//   clear them. The 'Revy' synthetic bot owns the listings.
//   7b-3 (autonomous loop + gamble-buy): arm a background loop (market-bot.json
//   enabled) that each tick tops listings up toward max_listings and runs a
//   gamble-buy pass — buying cheap player orders (price <= our price x
//   buy_threshold) gated by a d-die roll. "Run buy tick now" triggers one pass.
//
// Server-wide economy writes. Backed by GET /api/market(+/bot) and
// POST /api/market/{post,clear,buy,config}.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";
import { useAutoRefresh } from "./live";
import {
  fetchMarket,
  fetchMarketBot,
  marketBuy,
  marketClear,
  marketConfig,
  marketPost,
  type MarketBotStatus,
  type MarketConfig,
  type MarketInfo,
  type PublishResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

function logResult(setEntries: SetEntries, label: string, res: { ok: boolean; body: unknown } | null): boolean {
  if (!res) {
    pushToConsole(setEntries, label, "request failed (network error)", false);
    return false;
  }
  const b: unknown = res.body;
  const isObj = typeof b === "object" && b !== null;
  const ok = res.ok && isObj && "ok" in b && (b as { ok?: unknown }).ok === true;
  let msg: PublishResult | string;
  if (typeof b === "string") {
    msg = b;
  } else if (isObj && ("stdout" in b || "stderr" in b)) {
    msg = b as PublishResult;
  } else if (isObj && "error" in b && (b as { error?: string }).error) {
    msg = String((b as { error?: string }).error);
  } else if (isObj) {
    msg = JSON.stringify(b);
  } else {
    msg = String(b);
  }
  pushToConsole(setEntries, label, msg, ok);
  return ok;
}

const CFG_DEFAULTS: Required<Omit<MarketConfig, "disabled_items">> = {
  enabled: false,
  max_listings: 200,
  buy_threshold: 1.05,
  gamble_die: 12,
  gamble_target: 5,
  max_buys_per_tick: 5,
  scan_interval_secs: 300,
};

export function MarketTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [bot, setBot] = useState<MarketBotStatus | null>(null);
  const [info, setInfo] = useState<MarketInfo | null>(null);
  const [limit, setLimit] = useState(50);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState<null | { onConfirm: () => void }>(null);
  const [cfg, setCfg] = useState<typeof CFG_DEFAULTS>(CFG_DEFAULTS);

  async function load() {
    const [b, m] = await Promise.all([fetchMarketBot().catch(() => null), fetchMarket().catch(() => null)]);
    if (b && b.ok && typeof b.body === "object" && b.body) setBot(b.body as MarketBotStatus);
    if (m && m.ok && typeof m.body === "object" && m.body) {
      const mi = m.body as MarketInfo;
      setInfo(mi);
      if (mi.config) setCfg({ ...CFG_DEFAULTS, ...mi.config });
    }
  }

  // Live refresh updates the read-only views only — never cfg, so it can't
  // clobber edits to the autonomous-loop config the operator is typing.
  async function refreshLive() {
    const [b, m] = await Promise.all([fetchMarketBot().catch(() => null), fetchMarket().catch(() => null)]);
    if (b && b.ok && typeof b.body === "object" && b.body) setBot(b.body as MarketBotStatus);
    if (m && m.ok && typeof m.body === "object" && m.body) setInfo(m.body as MarketInfo);
  }

  useEffect(() => {
    void load();
  }, []);
  useAutoRefresh(() => void refreshLive(), 15000);

  async function post() {
    setBusy("post");
    const res = await marketPost(limit).catch(() => null);
    setBusy("");
    if (logResult(setConsoleEntries, `market post ${limit}`, res)) void load();
  }

  async function clear() {
    setBusy("clear");
    const res = await marketClear().catch(() => null);
    setBusy("");
    if (logResult(setConsoleEntries, "market clear", res)) void load();
  }

  async function buyNow() {
    setBusy("buy");
    const res = await marketBuy().catch(() => null);
    setBusy("");
    if (logResult(setConsoleEntries, "market buy-tick", res)) void load();
  }

  async function saveConfig() {
    setBusy("config");
    const res = await marketConfig(cfg).catch(() => null);
    setBusy("");
    if (logResult(setConsoleEntries, cfg.enabled ? "market loop armed" : "market loop disarmed", res)) void load();
  }

  const cat = info?.catalog;
  const anyBusy = busy !== "";
  const armed = info?.config?.enabled === true;

  function num(key: keyof typeof CFG_DEFAULTS, label: string, min: number, max: number, step = 1) {
    return (
      <label className="flex flex-col gap-1 text-xs">
        <span className="text-slate-400">{label}</span>
        <input
          type="number" min={min} max={max} step={step} value={cfg[key] as number}
          onChange={(e) => {
            const v = step < 1 ? parseFloat(e.target.value) : parseInt(e.target.value, 10);
            setCfg({ ...cfg, [key]: Number.isNaN(v) ? min : Math.max(min, Math.min(max, v)) });
          }}
          className="input-field w-full font-mono"
        />
      </label>
    );
  }

  return (
    <div className="space-y-6">
      {/* ----- Seed / clear (7b-2) ----- */}
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="font-semibold">Catalog seeding</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Seeds the in-game exchange with NPC sell orders from the catalog, priced by the engine and mapped to the right category. Applies live (server-wide).
            </p>
          </div>
          <button className="btn-ghost text-xs" onClick={() => void load()} disabled={anyBusy}>refresh</button>
        </header>
        <div className="p-4 space-y-4">
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
            <span><span className="text-slate-500">Bot orders</span> <span className="font-mono text-spice-300">{bot?.bot_orders ?? "…"}</span></span>
            <span><span className="text-slate-500">All NPC</span> <span className="font-mono">{bot?.npc_orders ?? "…"}</span></span>
            <span><span className="text-slate-500">Player</span> <span className="font-mono">{bot?.player_orders ?? "…"}</span></span>
            {cat && <span><span className="text-slate-500">Catalog</span> <span className="font-mono">{cat.items}</span> ({cat.vendor_priced} priced)</span>}
            {bot?.exchange && <span className="text-slate-500 text-xs">exchange {bot.exchange} · owner {bot.owner}</span>}
          </div>

          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="label" htmlFor="mk-limit">Seed how many NPC orders</label>
              <input id="mk-limit" type="number" min={1} max={2000} value={limit}
                onChange={(e) => setLimit(Math.max(1, Math.min(2000, parseInt(e.target.value || "1", 10))))}
                className="input-field w-32 font-mono" />
            </div>
            <button className="btn-primary" disabled={anyBusy} onClick={() => void post()}>
              {busy === "post" ? "posting…" : `Seed ${limit} orders`}
            </button>
            <button className="btn-ghost border border-red-900/60 text-red-300" disabled={anyBusy}
              onClick={() => setConfirm({ onConfirm: () => void clear() })}>
              {busy === "clear" ? "clearing…" : "Clear all bot orders"}
            </button>
          </div>
          <p className="text-xs text-slate-500">
            Orders are owned by the synthetic bot and are buyable in-game. “Clear” removes only bot orders (never player listings).
          </p>
        </div>
      </div>

      {/* ----- Autonomous loop + gamble-buy (7b-3) ----- */}
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="font-semibold flex items-center gap-2">
              Autonomous loop
              <span className={armed ? "pill-ok" : "pill-warn"}>{armed ? "armed" : "off"}</span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              When armed, a background loop tops listings up toward <span className="font-mono">max_listings</span> and runs a gamble-buy pass every <span className="font-mono">{cfg.scan_interval_secs}s</span>.
            </p>
          </div>
        </header>
        <div className="p-4 space-y-4">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={cfg.enabled}
              onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} />
            <span>Enable autonomous loop (post top-up + gamble-buy)</span>
          </label>

          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {num("max_listings", "Listing target", 0, 100000)}
            {num("scan_interval_secs", "Tick interval (s)", 30, 86400)}
            {num("max_buys_per_tick", "Max buys / tick", 0, 10000)}
            {num("buy_threshold", "Buy threshold ×", 0, 100, 0.05)}
            {num("gamble_die", "Gamble die (d)", 1, 1000)}
            {num("gamble_target", "Gamble target ≤", 0, 1000)}
          </div>
          <p className="text-xs text-slate-500">
            Gamble-buy: buys player orders priced at or below our price × <span className="font-mono">{cfg.buy_threshold}</span>, each a roll on a <span className="font-mono">d{cfg.gamble_die}</span> that must land ≤ <span className="font-mono">{cfg.gamble_target}</span> (≈ {Math.round((Math.min(cfg.gamble_target, cfg.gamble_die) / cfg.gamble_die) * 100)}% per eligible order). The bot pays the seller and destroys the item. Set target 0 to post only.
          </p>

          <div className="flex flex-wrap items-center gap-3">
            <button className="btn-primary" disabled={anyBusy} onClick={() => void saveConfig()}>
              {busy === "config" ? "saving…" : cfg.enabled ? "Arm loop" : "Save (disarmed)"}
            </button>
            <button className="btn-ghost border border-slate-700" disabled={anyBusy} onClick={() => void buyNow()}>
              {busy === "buy" ? "buying…" : "Run buy tick now"}
            </button>
            <span className="text-xs text-slate-500">“Run buy tick now” executes one gamble-buy pass immediately, ignoring the enabled flag.</span>
          </div>
        </div>
      </div>

      <Confirm
        open={confirm !== null}
        title="Clear all market-bot orders?"
        message="Remove every NPC sell order the bot posted (and their backing items). Player listings are untouched."
        confirmLabel="Clear bot orders"
        onConfirm={() => { confirm?.onConfirm(); setConfirm(null); }}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
