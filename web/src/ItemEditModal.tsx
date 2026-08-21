// Inline editor for one item stack's quantity + quality (feature "item edit").
// Presentational: it owns the two input values (seeded from the item it opens
// on — the parent mounts it fresh per edit) and emits only the CHANGED fields
// so an untouched field is left alone by the backend COALESCE. The parent owns
// the API call, busy/error and closing. Styling mirrors <Confirm>.

import { useState } from "react";

export interface EditableItem {
  id: string;
  name: string;
  tmpl: string;
  qty: string;      // current stack_size, as text from the row
  quality: string;  // current quality_level, as text
}

function toInt(s: string): number | null {
  if (!/^\d+$/.test(s.trim())) return null;
  return parseInt(s, 10);
}

export function ItemEditModal({ item, busy, error, onSave, onCancel }: {
  item: EditableItem;
  busy: boolean;
  error: string | null;
  onSave: (patch: { stack?: number; quality?: number }) => void;
  onCancel: () => void;
}) {
  const [qty, setQty] = useState(item.qty || "");
  const [quality, setQuality] = useState(item.quality || "");

  const qtyN = toInt(qty);
  const qualN = toInt(quality);
  const qtyValid = qtyN !== null && qtyN >= 1;
  const qualValid = qualN !== null && qualN >= 0;
  const qtyChanged = qty.trim() !== (item.qty || "").trim();
  const qualChanged = quality.trim() !== (item.quality || "").trim();
  const canSave = qtyValid && qualValid && (qtyChanged || qualChanged) && !busy;

  function submit() {
    if (!canSave) return;
    const patch: { stack?: number; quality?: number } = {};
    if (qtyChanged && qtyN !== null) patch.stack = qtyN;
    if (qualChanged && qualN !== null) patch.quality = qualN;
    onSave(patch);
  }

  return (
    <div className="fixed inset-0 bg-slate-950/80 flex items-center justify-center p-4 z-50 animate-fade-in">
      <div className="card max-w-md w-full p-6">
        <h3 className="text-lg font-semibold text-slate-100 mb-1">Edit item stack</h3>
        <p className="text-[11px] text-slate-500 font-mono mb-4">{item.tmpl} · #{item.id}</p>

        <div className="grid grid-cols-2 gap-4 mb-4">
          <label className="block">
            <span className="text-xs text-slate-400">Quantity</span>
            <input
              className="input-field w-full mt-1 font-mono"
              inputMode="numeric"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              disabled={busy}
            />
            {!qtyValid && qty.trim() !== "" && (
              <span className="text-[10px] text-red-400">must be an integer ≥ 1</span>
            )}
          </label>
          <label className="block">
            <span className="text-xs text-slate-400">Quality</span>
            <input
              className="input-field w-full mt-1 font-mono"
              inputMode="numeric"
              value={quality}
              onChange={(e) => setQuality(e.target.value)}
              disabled={busy}
            />
            {!qualValid && quality.trim() !== "" && (
              <span className="text-[10px] text-red-400">must be an integer ≥ 0</span>
            )}
          </label>
        </div>

        <p className="text-[11px] text-slate-500 mb-4 leading-tight">
          Writes <span className="font-mono text-slate-300">dune.items</span> directly. The owner must be{" "}
          <span className="font-mono text-slate-300">offline</span> (world/base items need the map fully stopped) —
          the server rejects live edits. Quality is capped at the highest tier the world already has.
        </p>

        {error && <p className="text-sm text-red-400 mb-3">{error}</p>}

        <div className="flex justify-end gap-2">
          <button className="btn-ghost" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn-primary" onClick={submit} disabled={!canSave}>
            {busy ? "saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
