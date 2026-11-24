/**
 * 感恩节火锅 Potluck: the backend for altaran.xyz/hotpot
 *
 * Setup (about 5 minutes):
 * 1. Go to script.google.com > New project, signed in to only one Google account
 *    (an incognito window helps). Delete the sample code and paste this whole file in. Save.
 * 2. Choose setup in the function list and click Run. It creates the sheet and logs its link.
 * 3. Click Deploy > New deployment. For the type, choose "Web app".
 *      Execute as:      Me
 *      Who has access:  Anyone
 *    Click Deploy and allow the permissions it asks for.
 * 4. Copy the Web app URL (it ends in /exec) and paste it into API_URL in hotpot/index.html.
 *
 * Each sign-up is one row in the "signups" tab, which the script creates the first time it runs.
 * To delete an entry, delete its row in the sheet.
 * After you change this code, use Deploy > Manage deployments > Edit > New version,
 * so the URL stays the same.
 */

const SHEET_NAME = 'signups';
const HEADERS = ['id', 'token', 'name', 'heads', 'items_json', 'items', 'note', 'updatedAt'];
const REQ_SHEET = 'requests';
const REQ_HEADERS = ['id', 'token', 'by', 'cat', 'text', 'qty', 'claimedById', 'claimedBy', 'createdAt'];
const CATS = {
  broth: '锅底/汤底', meat: '肉类', sea: '海鲜', balls: '丸滑/豆制品', veg: '蔬菜菌菇',
  staple: '主食', dip: '蘸料', drink: '饮料/酒', dessert: '甜点/水果', gear: '锅具/餐具', other: '其他'
};

// The first run creates a sheet named 感恩节火锅报名 in the deploying account's Drive
// and remembers it. Run setup() once to create it and log its link.
function spreadsheet_() {
  const props = PropertiesService.getScriptProperties();
  const saved = props.getProperty('SHEET_ID');
  if (saved) return SpreadsheetApp.openById(saved);
  const ss = SpreadsheetApp.create('感恩节火锅报名');
  props.setProperty('SHEET_ID', ss.getId());
  return ss;
}

function setup() {
  const ss = spreadsheet_();
  sheet_();
  Logger.log('Sign-ups are stored in: ' + ss.getUrl());
}

function sheet_() {
  const ss = spreadsheet_();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.getSheets()[0];
    sh.setName(SHEET_NAME);
    sh.appendRow(HEADERS);
    sh.setFrozenRows(1);
    sh.hideColumns(2); // token column
  }
  return sh;
}

function reqSheet_() {
  const ss = spreadsheet_();
  let sh = ss.getSheetByName(REQ_SHEET);
  if (!sh) {
    sh = ss.insertSheet(REQ_SHEET);
    sh.appendRow(REQ_HEADERS);
    sh.setFrozenRows(1);
    sh.hideColumns(2); // token column
  }
  return sh;
}

function reqRows_() {
  const values = reqSheet_().getDataRange().getValues();
  const out = [];
  for (let r = 1; r < values.length; r++) {
    const v = values[r];
    if (!v[0]) continue;
    out.push({ row: r + 1, id: String(v[0]), token: String(v[1]), by: String(v[2]), cat: String(v[3]), text: String(v[4]),
               qty: String(v[5] || ''), claimedById: String(v[6] || ''), claimedBy: String(v[7] || ''),
               createdAt: v[8] ? new Date(v[8]).toISOString() : '' });
  }
  return out;
}

function publicReqs_() {
  return reqRows_().map(function (r) {
    return { id: r.id, by: r.by, cat: r.cat, text: r.text, qty: r.qty, claimedById: r.claimedById, claimedBy: r.claimedBy, createdAt: r.createdAt };
  });
}

// Mark the requests this entry's items point at as claimed by it, and release ones it dropped.
function syncClaims_(entryId, name, reqIds) {
  const sh = reqSheet_();
  reqRows_().forEach(function (r) {
    const want = reqIds.indexOf(r.id) >= 0;
    if (want && (!r.claimedById || r.claimedById === entryId)) {
      sh.getRange(r.row, 7, 1, 2).setValues([[entryId, cell_(name)]]);
    } else if (!want && r.claimedById === entryId) {
      sh.getRange(r.row, 7, 1, 2).setValues([['', '']]);
    }
  });
}

function state_(extra) {
  const out = { ok: true, entries: publicList_(), requests: publicReqs_() };
  Object.keys(extra || {}).forEach(function (k) { out[k] = extra[k]; });
  return out;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function rows_() {
  const sh = sheet_();
  const values = sh.getDataRange().getValues();
  const out = [];
  for (let r = 1; r < values.length; r++) {
    const v = values[r];
    if (!v[0]) continue;
    let items = [];
    try { items = JSON.parse(v[4] || '[]'); } catch (e) {}
    out.push({ row: r + 1, id: String(v[0]), token: String(v[1]), name: String(v[2]), heads: Number(v[3]) || 1,
               items: items, note: String(v[6] || ''), updatedAt: v[7] ? new Date(v[7]).toISOString() : '' });
  }
  return out;
}

function publicList_() {
  return rows_().map(function (e) {
    return { id: e.id, name: e.name, heads: e.heads, items: e.items, note: e.note, updatedAt: e.updatedAt };
  });
}

function clean_(s, max) { return String(s == null ? '' : s).replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, max); }
// Keep guest text from being read as a spreadsheet formula.
function cell_(s) { return /^[=+\-@]/.test(s) ? "'" + s : s; }

function doGet() {
  return json_(state_());
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return json_({ ok: false, error: 'bad_request' }); }

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sh = sheet_();
    const all = rows_();

    if (body.action === 'delete') {
      const hit = all.find(function (x) { return x.id === body.id && x.token === body.token; });
      if (!hit) return json_({ ok: false, error: 'not_found' });
      sh.deleteRow(hit.row);
      syncClaims_(hit.id, '', []);
      return json_(state_());
    }

    if (body.action === 'addRequest') {
      const by = clean_(body.by, 40), text = clean_(body.text, 60);
      if (!by || !text) return json_({ ok: false, error: 'missing_fields' });
      const rsh = reqSheet_();
      if (rsh.getLastRow() > 100) return json_({ ok: false, error: 'full' });
      const rid = 'r' + Utilities.getUuid().slice(0, 7);
      const rtoken = Utilities.getUuid();
      rsh.appendRow([rid, rtoken, cell_(by), CATS[body.cat] ? body.cat : 'other', cell_(text), cell_(clean_(body.qty, 20)), '', '', new Date()]);
      return json_(state_({ id: rid, token: rtoken }));
    }

    if (body.action === 'deleteRequest') {
      const r = reqRows_().find(function (x) { return x.id === body.id && x.token === body.token; });
      if (!r) return json_({ ok: false, error: 'not_found' });
      reqSheet_().deleteRow(r.row);
      return json_(state_());
    }

    if (body.action === 'save') {
      const name = clean_(body.name, 40);
      const heads = Math.max(1, Math.min(20, parseInt(body.heads, 10) || 1));
      const note = String(body.note == null ? '' : body.note).trim().slice(0, 300);
      const items = (Array.isArray(body.items) ? body.items : []).slice(0, 20).map(function (i) {
        return { cat: CATS[i && i.cat] ? i.cat : 'other', text: clean_(i && i.text, 60), qty: clean_(i && i.qty, 20), reqId: clean_(i && i.reqId, 20) };
      }).filter(function (i) { return i.text; });
      if (!name || !items.length) return json_({ ok: false, error: 'missing_fields' });

      const itemsText = items.map(function (i) { return CATS[i.cat] + '：' + i.text + (i.qty ? '（' + i.qty + '）' : ''); }).join('；');
      const now = new Date();
      let id = body.id, token = body.token;
      const hit = id && all.find(function (x) { return x.id === id && x.token === token; });
      if (hit) {
        sh.getRange(hit.row, 3, 1, 6).setValues([[cell_(name), heads, JSON.stringify(items), itemsText, cell_(note), now]]);
      } else {
        if (all.length >= 200) return json_({ ok: false, error: 'full' });
        id = Utilities.getUuid().slice(0, 8);
        token = Utilities.getUuid();
        sh.appendRow([id, token, cell_(name), heads, JSON.stringify(items), itemsText, cell_(note), now]);
      }
      syncClaims_(id, name, items.map(function (i) { return i.reqId; }).filter(Boolean));
      return json_(state_({ id: id, token: token }));
    }

    return json_({ ok: false, error: 'unknown_action' });
  } finally {
    lock.releaseLock();
  }
}
