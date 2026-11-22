// Run with: node --test tests/dashboard_model.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const model = vm.runInNewContext(readFileSync('web/static/js/dashboard-model.js','utf8')+'; Dashboard;');
const history = [100000,101000,99500,99000,98000,97000].map((equity,i)=>({date:`2026-01-${String(i+12).padStart(2,'0')}`,equity}));

test('closed-market day uses completed closes even when the broker day change resets',()=>{
  const result = model.performance({equity:97000,last_equity:97000,valuation:{market_open:false}},history);
  assert.equal(result.day.amount,-1000);
  assert.equal(result.day.end,'2026-01-17');
  assert.equal(result.fiveDays.amount,-3000);
  assert.equal(result.total.amount,-3000);
});
test('open-market change uses current broker marks, not the completed-day curve',()=>{
  const result = model.performance({equity:97500,last_equity:97000,valuation:{market_open:true}},history);
  assert.equal(result.day.amount,500);
  assert.equal(result.fiveDays.amount,-3000);
  assert.equal(result.intraday,true);
});
test('missing values do not become zero gains or a healthy bot',()=>{
  const result = model.performance({},[]);
  assert.equal(result.total,null);
  assert.equal(result.day,null);
  assert.equal(result.fiveDays,null);
  assert.equal(model.money(null),'—');
  assert.equal(model.percent(undefined),'—');
  assert.equal(model.health({}).label,'Bot status unavailable');
  assert.equal(model.strategyState('HEALTHY',null).label,'Holdings unavailable');
});
test('five-day return requires six distinct closes; malformed rows are not used',()=>{
  const rows = [...history.slice(0,5), history[4], {date:'2026-01-18',equity:null},{date:'2026-01-19',equity:'NaN'}].reverse();
  const result = model.performance({},rows);
  assert.equal(result.fiveDays,null);
  assert.equal(result.curve.length,5);
});
test('portfolio and strategy stops override normal-looking holdings and health',()=>{
  assert.equal(model.health({status:'HEALTHY',killed:true}).label,'Trading stopped');
  assert.equal(model.health({status:'HEALTHY',issues:['risk cycle overdue']}).label,'Bot needs attention');
  assert.equal(model.health({status:'HALTED_TODAY'}).label,'Trading paused today');
  assert.equal(model.strategyState('FAST_TRIPPED',true).label,'Risk pause');
  assert.equal(model.strategyState('DISABLED',false).label,'Switched off');
});
test('research data limits are distinct from bot health and verification failures',()=>{
  const lab = {pipeline:{status:'ACTION_REQUIRED',issues:['Real-time consolidated SIP data is unavailable']}};
  assert.equal(model.research(lab).label,'Research needs better price data');
  assert.equal(model.health({status:'HEALTHY'}).label,'Bot running normally');
  lab.production_verification={simulator_comparison:{status:'MISMATCH'}};
  assert.equal(model.research(lab).label,'Research checks need attention');
  assert.equal(model.research(null).label,'Research status unavailable');
  assert.equal(model.research({report_stale:true}).label,'Research report is behind');
});
