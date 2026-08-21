// Guild management (dune-admin #117 port, MIT — see ATTRIBUTION.md).
// Reads list every guild; selecting one loads its roster + pending invites.
// All mutations go through the game's own guild procs (self-locking +
// pg_notify → the running maps apply them LIVE): describe, role transfer
// (100 = the single leader slot; promoting demotes the sitting leader to 50),
// kick (the proc silently skips leaders, so the backend pre-checks and
// refuses loudly), disband. Rename is the one lock-guarded UPDATE — no game
// proc or notify verb exists, so the game shows the new name after its next
// restart; the DB is authoritative immediately.

import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import {
  fetchGuildDetail,
  fetchGuilds,
  guildCreate,
  guildDescribe,
  guildDisband,
  guildKick,
  guildRename,
  guildSetRole,
  type GuildDetailResp,
  type GuildInviteRow,
  type GuildMemberRow,
  type GuildRow,
  type PublishResult,
} from "./api";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";

interface ConfirmSpec {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
}

function lastErrLine(rb: unknown): string {
  if (typeof rb !== "object" || rb === null) return "request failed";
  const b = rb as { stderr?: string; error?: string };
  const line = (b.stderr || "").trim().split("\n").filter(Boolean).pop();
  return line || b.error || "request failed";
}

export function GuildsTab({ setConsoleEntries }: {
  setConsoleEntries: Dispatch<SetStateAction<ConsoleEntry[]>>;
}) {
  const [guilds, setGuilds] = useState<GuildRow[]>([]);
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<GuildRow | null>(null);
  const [members, setMembers] = useState<GuildMemberRow[]>([]);
  const [invites, setInvites] = useState<GuildInviteRow[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [newLeader, setNewLeader] = useState("");
  const [newName, setNewName] = useState("");

  async function load() {
    setLoading(true);
    const res = await fetchGuilds();
    setLoading(false);
    if (res.ok) {
      const b = res.body as { available: boolean; guilds: GuildRow[] };
      setAvailable(b.available);
      setGuilds(b.guilds || []);
    }
  }

  async function loadDetail(g: GuildRow) {
    setDetailLoading(true);
    const res = await fetchGuildDetail(g.guild_id);
    setDetailLoading(false);
    if (res.ok) {
      const b = res.body as GuildDetailResp;
      setMembers(b.members || []);
      setInvites(b.invites || []);
    } else {
      setMembers([]);
      setInvites([]);
    }
  }

  useEffect(() => { void load(); }, []);

  function select(g: GuildRow) {
    setSelected(g);
    setEditName(g.guild_name);
    setEditDesc(g.description);
    setErr(null);
    void loadDetail(g);
  }

  async function mutate(label: string, run: () => Promise<{ ok: boolean; body: unknown }>, reloadList = false) {
    setBusy(true);
    setErr(null);
    const res = await run().catch(() => null);
    setBusy(false);
    const rb = res?.body;
    const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
      && "ok" in rb && (rb as { ok?: unknown }).ok === true;
    pushToConsole(setConsoleEntries, label, (rb ?? "request failed") as PublishResult, ok);
    if (!ok) setErr(lastErrLine(rb));
    if (ok) {
      if (reloadList) { setSelected(null); void load(); }
      else if (selected) { void load(); void loadDetail(selected); }
    }
    return ok;
  }

  const leader = members.find((m) => m.role_id === "100");

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="font-semibold">🛡 Guilds</h2>
            <p className="text-xs text-slate-500">
              Player guilds with roster + roles. Role changes, kicks and disbands apply{" "}
              <span className="text-slate-300">live</span> (the game's own procedures notify the running maps);
              a rename shows in-game after the next server restart.
            </p>
          </div>
          <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
            {loading ? "…" : "refresh"}
          </button>
        </header>
        <div className="card-body">
          {!available && (
            <p className="text-sm text-amber-400">Guild tables are not present on this build.</p>
          )}
          {available && guilds.length === 0 && !loading && (
            <p className="text-sm text-slate-500 italic">No guilds on this server yet.</p>
          )}
          <div className="flex flex-wrap items-end gap-2 mb-3">
            <label className="block text-xs">
              <span className="text-slate-400">Leader (FLS id or name:Character)</span>
              <input className="input-field mt-1 w-56" value={newLeader} disabled={busy}
                placeholder="name:Sergentval" onChange={(e) => setNewLeader(e.target.value)} />
            </label>
            <label className="block text-xs">
              <span className="text-slate-400">Guild name</span>
              <input className="input-field mt-1 w-56" value={newName} disabled={busy}
                maxLength={64} onChange={(e) => setNewName(e.target.value)} />
            </label>
            <button className="btn-primary text-xs" disabled={busy || !newLeader.trim() || !newName.trim()}
              onClick={() => void mutate(`guild-create ${newName.trim()}`,
                () => guildCreate(newLeader.trim(), newName.trim()), true)
                .then((ok) => { if (ok) { setNewLeader(""); setNewName(""); } })}>
              create guild
            </button>
          </div>
          {err && !selected && <p className="text-sm text-red-400 mb-2">{err}</p>}
          {guilds.length > 0 && (
            <div className="overflow-x-auto">
              <table className="tbl text-xs">
                <thead>
                  <tr><th>Guild</th><th>Faction</th><th>Members</th><th>Description</th></tr>
                </thead>
                <tbody>
                  {guilds.map((g) => (
                    <tr key={g.guild_id}
                      className={"cursor-pointer border-t border-slate-800/70 hover:bg-slate-800/40 " +
                        (selected?.guild_id === g.guild_id ? "bg-slate-800/60" : "")}
                      onClick={() => select(g)}>
                      <td className="py-1 pr-3">
                        <span className="text-slate-200">{g.guild_name}</span>{" "}
                        <span className="text-[10px] text-slate-600 font-mono">#{g.guild_id}</span>
                      </td>
                      <td className="pr-3">{g.faction}</td>
                      <td className="pr-3 font-mono">{g.members}</td>
                      <td className="pr-3 text-slate-500 max-w-[20rem] truncate" title={g.description}>{g.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {selected && (
        <div className="card">
          <header className="card-header">
            <h3 className="card-title">{selected.guild_name} <span className="text-xs text-slate-500 font-mono">#{selected.guild_id}</span></h3>
            <button className="btn-danger text-xs" disabled={busy}
              onClick={() => setConfirm({
                title: "Disband guild?",
                message: `Dissolve "${selected.guild_name}" (#${selected.guild_id}). Every member is ejected immediately in-game. Unrecoverable.`,
                confirmLabel: "Disband",
                onConfirm: () => void mutate(`guild-disband ${selected.guild_id}`,
                  () => guildDisband(selected.guild_id), true),
              })}>
              disband
            </button>
          </header>
          <div className="card-body space-y-4">
            <div className="grid md:grid-cols-2 gap-3">
              <label className="block text-xs">
                <span className="text-slate-400">Name <span className="text-slate-600">(in-game after restart)</span></span>
                <div className="flex gap-2 mt-1">
                  <input className="input-field flex-1" value={editName} disabled={busy}
                    onChange={(e) => setEditName(e.target.value)} maxLength={64} />
                  <button className="btn-ghost text-xs" disabled={busy || !editName.trim() || editName.trim() === selected.guild_name}
                    onClick={() => void mutate(`guild-rename ${selected.guild_id}`,
                      () => guildRename(selected.guild_id, editName.trim()))}>
                    rename
                  </button>
                </div>
              </label>
              <label className="block text-xs">
                <span className="text-slate-400">Description <span className="text-slate-600">(live)</span></span>
                <div className="flex gap-2 mt-1">
                  <input className="input-field flex-1" value={editDesc} disabled={busy}
                    onChange={(e) => setEditDesc(e.target.value)} maxLength={512} />
                  <button className="btn-ghost text-xs" disabled={busy || editDesc === selected.description}
                    onClick={() => void mutate(`guild-describe ${selected.guild_id}`,
                      () => guildDescribe(selected.guild_id, editDesc))}>
                    save
                  </button>
                </div>
              </label>
            </div>

            {err && <p className="text-sm text-red-400">{err}</p>}

            <div>
              <div className="text-xs font-medium text-slate-300 mb-1">
                Roster {detailLoading ? "…" : `(${members.length})`}
              </div>
              <div className="overflow-x-auto">
                <table className="tbl text-xs">
                  <thead>
                    <tr><th>Character</th><th>Role</th><th>Status</th><th></th></tr>
                  </thead>
                  <tbody>
                    {members.map((m) => (
                      <tr key={m.player_id} className="border-t border-slate-800/70">
                        <td className="py-1 pr-3">
                          <span className="text-slate-200">{m.character || `(player ${m.player_id})`}</span>{" "}
                          <span className="text-[10px] text-slate-600 font-mono">#{m.player_id}</span>
                          {m.canonical === "f" && (
                            <span className="ml-1 text-[10px] text-amber-400" title="Row does not name an account's player_controller_id — the game ignores it">⚠ non-canonical</span>
                          )}
                        </td>
                        <td className="pr-3">
                          {m.role_id === "100"
                            ? <span className="text-spice-300">★ leader</span>
                            : <span className="text-slate-400">{m.role}</span>}
                        </td>
                        <td className="pr-3">
                          <span className={m.online_status === "Online" ? "text-emerald-400" : "text-slate-500"}>
                            {m.online_status || "—"}
                          </span>
                        </td>
                        <td className="text-right whitespace-nowrap">
                          {m.role_id !== "100" && (
                            <>
                              <button className="btn-ghost text-xs" disabled={busy}
                                title="Transfer leadership to this member (current leader becomes member)"
                                onClick={() => setConfirm({
                                  title: "Transfer leadership?",
                                  message: `Make "${m.character || m.player_id}" the leader of "${selected.guild_name}". ${leader ? `Current leader "${leader.character || leader.player_id}" becomes a member.` : ""} Applies live.`,
                                  confirmLabel: "Transfer",
                                  onConfirm: () => void mutate(`guild-set-role ${selected.guild_id} ${m.player_id} 100`,
                                    () => guildSetRole(selected.guild_id, m.player_id, 100)),
                                })}>
                                ★ make leader
                              </button>
                              <button className="btn-ghost btn-del text-xs ml-1" disabled={busy}
                                onClick={() => setConfirm({
                                  title: "Kick member?",
                                  message: `Remove "${m.character || m.player_id}" from "${selected.guild_name}". Applies live.`,
                                  confirmLabel: "Kick",
                                  onConfirm: () => void mutate(`guild-kick ${selected.guild_id} ${m.player_id}`,
                                    () => guildKick(selected.guild_id, m.player_id)),
                                })}>
                                kick
                              </button>
                            </>
                          )}
                        </td>
                      </tr>
                    ))}
                    {members.length === 0 && !detailLoading && (
                      <tr><td className="py-1 text-slate-500 italic" colSpan={4}>Empty roster.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {invites.length > 0 && (
              <div>
                <div className="text-xs font-medium text-slate-300 mb-1">Pending invites ({invites.length})</div>
                <ul className="text-xs text-slate-400 space-y-0.5">
                  {invites.map((iv) => (
                    <li key={iv.invite_id}>
                      <span className="text-slate-300">{iv.invitee || iv.invitee_id}</span>
                      {" "}invited by <span className="text-slate-300">{iv.sender || iv.sender_id}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      <Confirm
        open={confirm !== null}
        title={confirm?.title ?? ""}
        message={confirm?.message ?? ""}
        confirmLabel={confirm?.confirmLabel ?? ""}
        onConfirm={() => { confirm?.onConfirm(); setConfirm(null); }}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
