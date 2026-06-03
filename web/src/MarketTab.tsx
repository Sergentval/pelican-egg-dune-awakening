// Market tab: the 7b market-bot. Seeds the in-game exchange with NPC sell orders
// from the priced + category-mapped catalog, and clears them. Server-wide economy
// write (the synthetic 'Revy' bot owns the listings). Backed by GET /api/market,
// GET /api/market/bot, POST /api/market/{post,clear}.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";
import {
  fetchMarket,
  fetchMarketBot,
  marketClear,
  marketPost,
  type MarketBotStatus,
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
  const err = isObj && "error" in b ? (b as { error?: string }).error : undefined;
  pushToConsole(setEntries, label, typeof b === "string" ? b : (err ?? (b as PublishResult)), ok);
  return ok;
}

export function MarketTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [bot, setBot] = useState<MarketBotStatus | null>(null);
  const [info, setInfo] = useState<MarketInfo | null>(null);
  const [limit, setLimit] = useState(50);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState<null | { onConfirm: () => void }>(null);

  async function load() {
    const [b, m] = await Promise.all([fetchMarketBot().catch(() => null), fetchMarket().catch(() => null)]);
    if (b && b.ok && typeof b.body === "object" && b.body) setBot(b.body as MarketBotStatus);
    if (m && m.ok && typeof m.body === "object" && m.body) setInfo(m.body as MarketInfo);
  }

  useEffect(() => {
    void load();
  }, []);

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

  const cat = info?.catalog;
  const anyBusy = busy !== "";

  return (
    <div className="space-y-6">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="font-semibold">Market bot</h2>
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
