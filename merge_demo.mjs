// Fires one merge via the game's own functions.
//
// Usage:
//   node merge_demo.mjs                        # list mergeable clusters
//   node merge_demo.mjs fromCol fromRow toCol toRow   # perform the merge
//
// Verified live: 4x wheat t1 -> wheat t2 + 1 leftover; 3x chicken t1 -> chicken t2.

import { CDP, attach, evalIn, findGameTarget, WS_URL } from "./cdp_lib.mjs";

const cdp = new CDP(WS_URL);
await cdp.connect();

const target = await findGameTarget(cdp);
if (!target) throw new Error("game frame target not found — open the Discord activity first");

const sid = await attach(cdp, target.targetId);
const hasFmv = await evalIn(cdp, sid, "!!window.FMV");
if (!hasFmv.result.value) {
  throw new Error("window.FMV not installed — run install_fmv.mjs first");
}

const args = process.argv.slice(2);

if (args.length === 0) {
  const res = await evalIn(
    cdp,
    sid,
    `(function(){
      const S = window.FMV.services();
      const I = window.FMV.I();
      const cells = [...S.mapGrid._cells.values()]
        .filter(c => c && c.content && c.content.hasBehavior(I.Mergeable));
      const found = [];
      for (const to of cells) {
        const spec = to.content.getBehavior(I.Mergeable).targetSpecification;
        const chain = S.gridFilter.getAdjacentObjectsWithSameID(to, spec, undefined, [I.Mergeable])
          .filter(c => c !== undefined);
        if (chain.length >= 3) {
          const info = to.content.getObjectIdAndTier();
          found.push({ col: to.column, row: to.row, id: info.id, tier: info.tier,
                       chainLen: chain.length - 0 });
        }
      }
      const seen = new Set();
      return found.filter(f => { const k = f.col + ':' + f.row + ':' + f.id + f.tier;
        if (seen.has(k)) return false; seen.add(k); return true; }).slice(0, 10);
    })()`
  );
  console.log("mergeable clusters (chain incl. start cell):");
  console.log(JSON.stringify(res.result.value, null, 2));
  console.log("\nFire one with: node merge_demo.mjs fromCol fromRow toCol toRow");
} else {
  const [fromCol, fromRow, toCol, toRow] = args.map(Number);
  const res = await evalIn(
    cdp,
    sid,
    `window.FMV.merge(${fromCol}, ${fromRow}, ${toCol}, ${toRow})`
  );
  console.log("merge result:", JSON.stringify(res.result.value));
  if (res.result.value.ok) {
    await new Promise((r) => setTimeout(r, 2000));
    const board = await evalIn(
      cdp,
      sid,
      `(function(){ const b = window.FMV.board().filter(i => i.mergeable);
        return b.filter(i => i.col === ${fromCol} && i.row === ${fromRow} ||
                             i.col === ${toCol} && i.row === ${toRow}); })()`
    );
    console.log("cells after merge:", JSON.stringify(board.result.value));
  }
}

cdp.close();
