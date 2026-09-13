const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
let count = 0;

function simulate(template, scenario = {}) {
  const w = structuredClone(template);
  const nodes = Object.fromEntries(w.nodes.map(n => [n.name, n]));
  nodes.CONFIG1.parameters.jsCode = nodes.CONFIG1.parameters.jsCode
    .replace('YOUR_WIKI_NODE_TOKEN','wiki_test').replace('YOUR_TABLE_ID','tbl_test')
    .replace('YOUR_WORKER_HOST','worker.example.com').replace('YOUR_DRIVE_FOLDER_ID','folder_test');
  const history = {}, calls = [], visited = [];
  let clock = 1700000000000;
  const $ = name => {
    if (!history[name]) throw Error('Node not executed: ' + name);
    return {first: () => history[name][0], item: history[name][0]};
  };
  const MockDate = class extends Date { static now() { return clock; } };
  const execute = (js, input) => new Function('$json','$','$input','$execution','Date',js)(
    input[0]?.json || {}, $, {all: () => input}, {id: 'test_execution'}, MockDate);
  const evaluate = (value, input) => {
    if (typeof value === 'string' && value.startsWith('=')) {
      if (value.startsWith('={{') && value.endsWith('}}'))
        return execute('return ('+value.slice(3,-2)+');',input);
      return value.slice(1).replace(/\{\{([\s\S]*?)\}\}/g, (_,expression) => execute('return ('+expression+');',input));
    }
    if (Array.isArray(value)) return value.map(x => evaluate(x,input));
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,evaluate(v,input)]));
    return value;
  };
  let name = 'Feishu Generate Button1';
  let input = [{json:{body:{record_id:'recTest123'}}}];
  for (let step=0; step<3000 && name; step++) {
    const n = nodes[name]; visited.push(name);
    let output=input, branch=0;
    try {
      if (name === scenario.transportFailure) throw Error('Simulated transport failure');
      if (n.type.endsWith('.code')) output=execute(n.parameters.jsCode,input);
      else if (n.type.endsWith('.if')) {
        const condition=evaluate(n.parameters.conditions.conditions[0],input);
        const value=condition.leftValue;
        const pass=condition.operator.operation === 'notEmpty' ? !!value : value === true;
        branch=pass ? 0 : 1;
      } else if (n.type.endsWith('.wait')) clock += 20000;
      else if (n.type.endsWith('.httpRequest')) {
        const params=evaluate(n.parameters,input);
        calls.push({name,params});
        let json;
        if (name.includes('Get Token') || name==='Get Feishu Tenant Token1') json={code:0,tenant_access_token:'test-token'};
        else if (name.includes('Get Bitable') || name==='Get Wiki Bitable Token1') json={code:0,data:{node:{obj_token:'base_test'}}};
        else if (name==='Get Feishu Record1') json={code:0,data:{record:{record_id:'recTest123',fields:{
          '序号':1,'模板视频': scenario.missingVideo ? [] : [{url:'https://example.com/input.mp4',name:'demo.mp4'}],
          '人物参考图':[{url:'https://example.com/reference.jpg',name:'demo.jpg'}],
          '人物性别':'女','状态':scenario.busy ? '生成中' : '待生成'
        }}}};
        else if (name==='Submit Python Video Job1') json=scenario.missingJobId ? {} : {job_id:'job_test123'};
        else if (name==='Get Video Job Status1') json={status:scenario.status || 'completed',
          ...(scenario.createdAt === undefined ? {} : {created_at:scenario.createdAt}),caption:'示例文案',tags:['演示'],
          ...(scenario.metadataError ? {metadata_error:'provider detail'} : {})};
        else if (name==='Download Result Video1') json={};
        else json={code:0,data:{}};
        if (name===scenario.businessFailure) json={code:999,msg:'private provider message'};
        output=[{json}];
      } else if (n.type.endsWith('.googleDrive')) {
        evaluate(n.parameters,input);
        output=[{json:scenario.missingDriveId ? {} : {id:'drive_test',webViewLink:'https://drive.google.com/file/d/drive_test/view'}}];
      }
      history[name]=output;
    } catch (error) {
      if (n.onError==='continueErrorOutput') {branch=1; output=[{json:{error:error.message}}];}
      else return {calls,visited,history,error:error.message};
    }
    const links=w.connections[name]?.main?.[branch] || [];
    assert.ok(links.length<=1,'Harness expects a single sequential branch');
    name=links[0]?.node; input=output;
  }
  assert.ok(!name,'Workflow failed to terminate');
  return {calls,visited,history};
}

for (const mode of ['aws','local']) {
  const w=JSON.parse(fs.readFileSync(path.join(root,'workflows',mode+'.json')));
  const names=new Set(w.nodes.map(n=>n.name));
  assert.equal(names.size,w.nodes.length);
  for (const [source,connections] of Object.entries(w.connections)) {
    assert.ok(names.has(source));
    for(const branch of connections.main) for(const link of branch) assert.ok(names.has(link.node));
  }
  assert.equal(w.active,false);
  assert.deepEqual(w.pinData,{});
  assert.equal(w.nodes[0].parameters.authentication,'headerAuth');
  for(const n of w.nodes) {
    assert.equal(n.credentials,undefined);
    if(n.parameters.jsCode) new Function(n.parameters.jsCode);
  }
  const check=(label,scenario,test)=>{const result=simulate(w,scenario);test(result);count++;console.log('PASS',mode,label);};
  check('completed: correct payload, folder, original audio',{},r=>{
    assert.equal(r.error,undefined);
    assert.ok(r.visited.includes('Check Update Feishu - Completed1'));
    const raw=r.calls.find(c=>c.name==='Submit Python Video Job1').params.jsonBody;
    assert.equal(raw.generate_audio,false);
    assert.equal(raw.segment_seconds,mode==='local'?9:10);
    assert.ok(!raw.prompt.includes('Generate spoken audio'));
    assert.ok(!raw.prompt.includes('voice characteristics'));
    const completed=r.calls.find(c=>c.name==='Update Feishu - Completed1').params.jsonBody;
    assert.equal(completed.fields['状态'],'已完成');
    assert.equal('post_content' in completed.fields,mode==='aws');
  });
  check('duplicate does not submit or mark failed',{busy:true},r=>{
    assert.ok(r.visited.includes('Skip Duplicate')); assert.ok(!r.calls.some(c=>c.params.method==='PUT'));
    assert.ok(!r.visited.includes('Submit Python Video Job1'));
  });
  check('missing video is validated',{missingVideo:true},r=>{
    assert.ok(r.visited.includes('Update Feishu - Validation Failed1'));
    assert.ok(!r.visited.includes('Submit Python Video Job1'));
  });
  for (const status of ['failed','cancelled','expired']) check(status,{status},r=>{
    assert.ok(r.visited.includes('Update Feishu - Failed1'));
    assert.ok(r.error.includes('已回写飞书'));
  });
  for (const createdAt of [undefined,'2026-09-13T00:00:00Z',1700000000000,1700000000])
    check('timeout independent of timestamp '+String(createdAt),{status:'processing',createdAt},r=>{
      assert.ok(r.visited.includes('Update Feishu - Failed1'));
      const call=r.calls.find(c=>c.name==='Update Feishu - Failed1');
      assert.ok(call.params.jsonBody.fields['错误信息'].includes('等待超时'));
    });
  for (const transportFailure of ['Get Feishu Tenant Token1','Get Feishu Record1','Submit Python Video Job1',
      'Get Video Job Status1','Download Result Video1','Upload Result to Google Drive1','Update Feishu - Completed1'])
    check('recover '+transportFailure,{transportFailure},r=>{
      assert.ok(r.visited.includes('Recovery - Mark Failed'));
      assert.ok(r.error.includes('已回写飞书'));
    });
  for(const businessFailure of ['Get Feishu Tenant Token1','Get Wiki Bitable Token1','Get Feishu Record1','Update Feishu - Generating1','Update Feishu - Completed1'])
    check('Feishu business error '+businessFailure,{businessFailure},r=>{
      assert.ok(r.visited.includes('Recovery - Mark Failed'));
      assert.ok(r.error.includes('已回写飞书'));
    });
  for(const flag of ['missingJobId','missingDriveId']) check(flag,{[flag]:true},r=>{
    assert.ok(r.visited.includes('Recovery - Mark Failed'));
  });
  // Recovery itself must fail visibly instead of reporting success.
  check('failed writeback stays failed',{status:'failed',businessFailure:'Recovery - Get Token',transportFailure:'Update Feishu - Failed1'},r=>{
    assert.ok(r.error.includes('无法回写飞书'));
    assert.ok(!r.visited.includes('Recovery - Mark Failed'));
  });
}
console.log(`${count} offline workflow scenarios passed.`);
