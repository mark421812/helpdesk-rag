# U1 合成簽章向量：離線核對方法

這是[契約草案](./rag-upgrade-v1-contracts.md)的資料核對，不是已實作v2服務的contract/security tests。
只讀取[公開test key／自製fixture](./rag-upgrade-v1-golden-vectors.json)，不讀.env、公司文件、DB或呼叫Provider。
目前使用已存在的Python `rfc8785 0.1.4`／`cryptography 45.0.7`與Node `20.20.0`，沒有安裝新依賴。
兩份命令在workspace root執行，不import任何sibling service production package。

## Python：RFC8785與Ed25519

```bash
PYTHONDONTWRITEBYTECODE=1 answer-generation-service/.venv/bin/python - <<'PY'
import base64, hashlib, json
from copy import deepcopy
from pathlib import Path
import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

data = json.loads(Path('docs/rag-upgrade-v1-golden-vectors.json').read_text())
decode = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')
digest = lambda domain, obj: b64(hashlib.sha256(domain.encode() + rfc8785.dumps(obj)).digest())
key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(data['public_key_hex']))
normal = {c['id']: c for c in data['normal_cases']}
def verify(token):
    head, body, signature = token.split('.')
    key.verify(decode(signature), (head + '.' + body).encode('ascii'))

passed = 0
for case in normal.values():
    expected = case['expected']
    assert rfc8785.dumps(case['question']).decode() == expected['question_jcs_utf8']
    assert rfc8785.dumps(case['context']).decode() == expected['context_jcs_utf8']
    assert digest(data['question_domain_separator'], case['question']) == expected['question_digest']
    assert digest(data['context_domain_separator'], case['context']) == expected['context_digest']
    assert digest('helpdesk-rag/question/v1\0', case['question']) != expected['question_digest']
    assert digest('helpdesk-rag/context/v1\0', case['context']) != expected['context_digest']
    head, body, _ = expected['compact_jws'].split('.')
    assert decode(head) == rfc8785.dumps(case['header'])
    assert decode(body) == rfc8785.dumps(case['claims'])
    verify(expected['compact_jws'])
    passed += 1

resigned = 0
for test in data['negative_cases']:
    base = normal[test['base_case']]
    stage = test['expected_rejection_stage']
    if stage == 'digest':
        changed = deepcopy(base[test['target']])
        parent = changed
        for step in test['path'][:-1]:
            parent = parent[step]
        parent[test['path'][-1]] = test['value']
        domain = data[test['target'] + '_domain_separator']
        assert digest(domain, changed) != base['expected'][test['target'] + '_digest']
        passed += 1
    elif stage == 'signature':
        head, body, sig = base['expected']['compact_jws'].split('.')
        raw = bytearray(decode(sig)); raw[0] ^= 1
        rejected = False
        try:
            verify(head + '.' + body + '.' + b64(raw))
        except InvalidSignature:
            rejected = True
        assert rejected
        passed += 1
    else:
        # Valid crypto intentionally isolates the future semantic rejection test.
        verify(test['resigned_test_jws'])
        assert test['runtime_status'] == 'not_executed_runtime'
        resigned += 1

scope = data['normal_cases'][1]['context']['pinned_scopes'][0]
source = data['scope_fixture']
assert hashlib.sha256(rfc8785.dumps(source['structure'])).hexdigest() == scope['structure_sha256']
assert hashlib.sha256(data['scope_domain_separator'].encode() + rfc8785.dumps(source['manifest'])).hexdigest() == scope['scope_sha256']
assert sum('row_id' in member for member in source['manifest']['members']) == data['normal_cases'][1]['context']['verified_facts']['count']
print(json.dumps({'offline_vector_cases': passed, 'passed': passed, 'failed': 0, 'errors': 0, 'skipped': 0, 'resigned_fixture_signatures_verified': resigned, 'runtime_negative_cases_not_executed': resigned}))
PY
```

## Node：獨立canonicalization與公開key驗章

此fixture的JSON keys是ASCII，number皆safe integer；以下canonicalizer**只適用本fixture**，不是可部署的通用RFC8785 library。
Unicode value保持原樣，object keys按JavaScript UTF-16排序；正式服務仍使用已核准RFC8785套件，不複製這個簡化核對器。

```bash
node - <<'JS'
const fs = require('node:fs');
const crypto = require('node:crypto');
const data = JSON.parse(fs.readFileSync('docs/rag-upgrade-v1-golden-vectors.json', 'utf8'));
function canonical(value) {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && !Number.isSafeInteger(value)) throw Error('unsupported fixture number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (Object.keys(value).some(k => /[^\x00-\x7f]/.test(k))) throw Error('unsupported fixture key');
  return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}
const assert = (ok) => { if (!ok) throw Error('vector mismatch'); };
const digest = (domain, value) => crypto.createHash('sha256').update(domain).update(canonical(value)).digest('base64url');
const key = crypto.createPublicKey({key: Buffer.concat([Buffer.from('302a300506032b6570032100','hex'), Buffer.from(data.public_key_hex,'hex')]), format:'der', type:'spki'});
const verify = token => {
  const [head, body, sig] = token.split('.');
  return crypto.verify(null, Buffer.from(head + '.' + body, 'ascii'), key, Buffer.from(sig,'base64url'));
};
const normal = Object.fromEntries(data.normal_cases.map(c => [c.id,c]));
let passed = 0, resigned = 0;
for (const item of data.normal_cases) {
  assert(canonical(item.question) === item.expected.question_jcs_utf8);
  assert(canonical(item.context) === item.expected.context_jcs_utf8);
  assert(digest(data.question_domain_separator,item.question) === item.expected.question_digest);
  assert(digest(data.context_domain_separator,item.context) === item.expected.context_digest);
  assert(verify(item.expected.compact_jws)); passed++;
}
for (const test of data.negative_cases) {
  const base = normal[test.base_case];
  if (test.expected_rejection_stage === 'digest') {
    const changed = structuredClone(base[test.target]);
    let parent = changed;
    for (const step of test.path.slice(0,-1)) parent = parent[step];
    parent[test.path.at(-1)] = test.value;
    assert(digest(data[test.target + '_domain_separator'],changed) !== base.expected[test.target + '_digest']); passed++;
  } else if (test.expected_rejection_stage === 'signature') {
    const [head,body,sig] = base.expected.compact_jws.split('.');
    const bad = Buffer.from(sig,'base64url'); bad[0] ^= 1;
    assert(!verify(head + '.' + body + '.' + bad.toString('base64url'))); passed++;
  } else {
    assert(verify(test.resigned_test_jws)); resigned++;
  }
}
console.log(JSON.stringify({offline_vector_cases:passed,passed,failed:0,errors:0,skipped:0,resigned_fixture_signatures_verified:resigned,runtime_negative_cases_not_executed:resigned}));
JS
```

預期兩者都是12個離線cases通過，另8個semantic-negative fixture的簽章有效；那8個**拒絕行為尚未runtime執行**，不是skipped integration test。
不得把同一向量在兩種語言計算說成24題業務測試，也不得用這個結果宣告U1 review／U0品質已通過。
