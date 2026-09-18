import fs from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = process.cwd();
const python = process.env.PYTHON || "python";
const query = String.raw`
import json, sqlite3
conn=sqlite3.connect('data/mediaflow.db')
conn.row_factory=sqlite3.Row
rows=conn.execute("""
 SELECT p.id,a.name account,p.video_url,p.bvid,p.publish_status,p.verification_status,
        p.created_at,p.remote_comment_id,p.detail
 FROM promotion_records p JOIN accounts a ON a.id=p.account_id
 WHERE p.platform='bilibili' ORDER BY p.created_at,p.id
""").fetchall()
print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
`;
const rows = JSON.parse(execFileSync(python, ["-c", query], {
  cwd: root,
  encoding: "utf8",
  env: { ...process.env, PYTHONIOENCODING: "utf-8" },
}));

function priority(row) {
  if (row.publish_status === "failed" || row.publish_status === "失败") return "无需核验（本地失败）";
  if (["verified", "manual_verified"].includes(row.verification_status)) return "已通过本地核验";
  if (row.verification_status === "accepted_by_api") return "需人工核验（接口接受）";
  return "需人工核验（历史记录）";
}

const outputDir = `${root}/outputs/bilibili_comment_audit`;
await fs.mkdir(outputDir, { recursive: true });

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("留言核验");
sheet.showGridLines = false;
sheet.getRange("A2:H2").merge();
sheet.getRange("A2").values = [["哔哩哔哩历史推广留言核验清单"]];
sheet.getRange("A3").values = [["记录总数"]];
sheet.getRange("B3").values = [[rows.length]];
sheet.getRange("D3").values = [["导出时间"]];
sheet.getRange("E3").values = [[new Date().toISOString().slice(0, 19).replace("T", " ")]];

const headers = ["记录ID", "推广账户", "视频 URL", "BV号", "本地发布状态", "本地核验状态", "记录时间", "人工核验建议", "评论ID", "归档说明"];
sheet.getRange("A5:J5").values = [headers];
const data = rows.map((r) => [
  r.id, r.account, r.video_url, r.bvid, r.publish_status, r.verification_status,
  r.created_at, priority(r), r.remote_comment_id || "", r.detail || "",
]);
if (data.length) sheet.getRange(`A6:J${data.length + 5}`).values = data;
sheet.tables.add(`A5:J${data.length + 5}`, true, "BilibiliCommentAudit");

sheet.getRange("A2:J2").format = { font: { name: "Arial", size: 14, bold: true, color: "#1F2937" } };
sheet.getRange("A3:E3").format = { font: { name: "Arial", size: 10 }, fill: "#F3F4F6" };
sheet.getRange("A5:J5").format = { fill: "#1F4E78", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center", verticalAlignment: "center" };
sheet.getRange(`A6:J${data.length + 5}`).format = { font: { name: "Arial", size: 10 }, verticalAlignment: "center" };
sheet.getRange(`A6:A${data.length + 5}`).format.horizontalAlignment = "right";
sheet.getRange(`C6:C${data.length + 5}`).format.wrapText = false;
sheet.getRange(`J6:J${data.length + 5}`).format.wrapText = false;
sheet.getRange(`H6:H${data.length + 5}`).conditionalFormats.add("containsText", { text: "需人工核验", format: { fill: "#FFF2CC", font: { color: "#7F6000", bold: true } } });
sheet.getRange(`H6:H${data.length + 5}`).conditionalFormats.add("containsText", { text: "无需核验", format: { fill: "#FCE4D6", font: { color: "#9C0006" } } });
sheet.getRange("A:A").format.columnWidth = 10;
sheet.getRange("B:B").format.columnWidth = 28;
sheet.getRange("C:C").format.columnWidth = 45;
sheet.getRange("D:D").format.columnWidth = 18;
sheet.getRange("E:F").format.columnWidth = 18;
sheet.getRange("G:G").format.columnWidth = 20;
sheet.getRange("H:H").format.columnWidth = 26;
sheet.getRange("I:I").format.columnWidth = 16;
sheet.getRange("J:J").format.columnWidth = 44;
sheet.freezePanes.freezeRows(5);

workbook.recalculate();
const check = await workbook.inspect({ kind: "table", range: `留言核验!A2:J${Math.min(data.length + 5, 12)}`, include: "values,formulas", tableMaxRows: 12, tableMaxCols: 10 });
console.log(check.ndjson);
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 20 }, summary: "formula error scan" });
console.log(errors.ndjson);
const preview = await workbook.render({ sheetName: "留言核验", range: "A2:J15", scale: 1.2, format: "png" });
await fs.writeFile(`${outputDir}/preview.png`, new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(`${outputDir}/bilibili_comment_audit.xlsx`);
