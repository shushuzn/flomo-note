/**
 * flomo-workbench —— flomo-note 技能的 Harness 工具壳（Host 半）。
 *
 * 三条约束（与项目硬限一一对齐）：
 *
 *   1. **本文件不含任何业务规则。** 签名、查重、闸门凭证、格式校验、写后回读验收的
 *      唯一实现在 scripts/*.py，且各有回归用例。这里只做一件事：把「逐次跑脚本」
 *      变成一等工具。任何规则都不得在这里出现第二份实现。
 *   2. **一次工具调用 = 一次脚本执行。** 不提供批量 / 循环 / 并发参数——SKILL.md H3
 *      禁止把多次调用串成一批，工具层面就不该存在这种能力。
 *   3. **token 不进本进程。** 脚本自己读 .mcp.json 建连；本插件只传 argv、原样转传
 *      stdout/stderr，不解析、不落盘输出。
 *
 * 模式不写在配置里，而是问 ctx：ctx.agentPresets.composedPreset(ctx) 返回本插件所在
 * preset 的 id（flomo-write / flomo-tidy / flomo-audit）。体检模式据此**不注册写工具**
 * ——「写云」这个动作在体检会话里根本不存在，比在提示词里劝它别写更硬。
 * 取不到 preset 时按 fail-closed 处理（不注册写工具），理由见 resolveMode。
 *
 * 本地 bundle 不 import 任何 @deepseek-ai/* 包（profile 的 node_modules 里没有它们，
 * 裸 import 会在激活时解析失败），故工具定义用普通对象；parameters 的确切形态
 * （schemastery spec 还是已转换的 JSON Schema）无法离线确认，由 registerTool 试探，
 * 第一个被接受的形态胜出并打日志。确认后应把胜出形态固定、删掉试探梯子。
 */
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const name = 'flomo-workbench';
export const inject = ['tools', 'commands', 'agentPresets', 'workingDirectory'];

/** preset id → 模式名。 */
const MODE_OF_PRESET = { 'flomo-write': 'write', 'flomo-tidy': 'tidy', 'flomo-audit': 'audit' };
/** 模式名 → preset id（/flomo 命令用）。 */
const PRESET_OF_MODE = { write: 'flomo-write', tidy: 'flomo-tidy', audit: 'flomo-audit' };

const SCRIPT_TIMEOUT_MS = 180000;
const MAX_OUT_CHARS = 6000;

/** flomo_read 的 action → 云端工具名、可用参数、是否走 --brief。
 *  batch_get 刻意不 brief：H1 要求判断正文必须取全文，截断快照只能用于「发现有哪些卡」。 */
const READ_SPECS = {
  search: { tool: 'memo_search', fields: ['keywords', 'tag', 'limit'], brief: true },
  batch_get: { tool: 'memo_batch_get', fields: ['ids', 'slugs'], brief: false },
  recommended: { tool: 'memo_recommended', fields: ['id', 'limit'], brief: true },
  tag_tree: { tool: 'tag_tree', fields: ['limit'], brief: true, defaults: { limit: 2000 } },
  daily_review: { tool: 'get_daily_review', fields: [], brief: false },
  format_guide: { tool: 'get_format_guide', fields: [], brief: false },
  tag_guide: { tool: 'get_tag_guide', fields: [], brief: false },
};

/** flomo_scan 的 action → 仓库只读取证脚本。 */
const SCAN_SCRIPTS = {
  qualified_concepts: 'scan_qualified_concepts.py',
  concept_duplication: 'scan_concept_duplication.py',
};

// ---------------------------------------------------------------- 环境解析

/** 仓库根：以调用会话的工作目录为准，并要求 scripts/flomo_client.py 在场。 */
function repoRootOf(ctx, exec) {
  const candidates = [];
  try {
    const session = exec && exec.agent && exec.agent.session;
    if (session && ctx.workingDirectory && typeof ctx.workingDirectory.get === 'function') {
      candidates.push(ctx.workingDirectory.get(session));
    }
  } catch { /* 取不到工作目录时退回进程 cwd */ }
  candidates.push(process.cwd());
  for (const dir of candidates) {
    if (dir && existsSync(join(dir, 'scripts', 'flomo_client.py'))) return dir;
  }
  return null;
}

let _python; // undefined=未解析；''=解析失败（缓存，避免每次重试）
/** 选一个真能跑的 Python：显式 env > dsh 运行时内置 > PATH。逐个 `--version` 实跑验证。 */
function pythonBin() {
  if (_python !== undefined) return _python;
  const home = process.env.USERPROFILE || process.env.HOME || '';
  const candidates = [
    process.env.FLOMO_PYTHON_BIN,
    home && join(home, '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
    'python',
    'python3',
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const probe = spawnSync(candidate, ['--version'], { windowsHide: true, timeout: 20000 });
      if (probe.status === 0) { _python = candidate; return _python; }
    } catch { /* 换下一个 */ }
  }
  _python = '';
  return _python;
}

/** 输出截断并显式标注（agent-experience：返回有界结果，不倾倒全文）。 */
function clip(text) {
  const s = String(text == null ? '' : text);
  if (s.length <= MAX_OUT_CHARS) return s;
  return s.slice(0, MAX_OUT_CHARS) + `\n…[已截断，原长度 ${s.length} 字符]`;
}

let _seq = 0;
/** 落一个当轮中间文件。命名沿用项目约定（memo_body* / _tmp/），由 cleanup.py 回收。 */
function writeTemp(root, prefix, text) {
  const dir = join(root, '_tmp');
  try { mkdirSync(dir, { recursive: true }); } catch { /* 已存在或不可建，让 writeFileSync 报错 */ }
  const path = join(dir, `${prefix}_${Date.now().toString(36)}_${(_seq++).toString(36)}.txt`);
  writeFileSync(path, String(text), 'utf8');
  return path;
}

/** 跑一个仓库脚本。**不走 shell**（argv 数组直传），彻底规避 H3 说的引号被转全角。 */
function runScript(python, root, script, args, timeoutMs) {
  const budget = timeoutMs || SCRIPT_TIMEOUT_MS;
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(python, [join('scripts', script), ...args], { cwd: root, windowsHide: true });
    } catch (error) {
      resolve({ ok: false, code: -1, stdout: '', stderr: `无法启动 ${python}: ${error && error.message}` });
      return;
    }
    let out = '';
    let err = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { child.kill(); } catch { /* 已退出 */ }
      resolve({ ok: false, code: -1, stdout: clip(out), stderr: `脚本超时（${budget}ms）已终止` });
    }, budget);
    child.stdout.on('data', (chunk) => { out += chunk; });
    child.stderr.on('data', (chunk) => { err += chunk; });
    child.on('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: false, code: -1, stdout: clip(out), stderr: `启动失败：${error && error.message}` });
    });
    child.on('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: code === 0, code: code == null ? -1 : code, stdout: clip(out), stderr: clip(err) });
    });
  });
}

// ---------------------------------------------------------------- 模式判定

/** 取所在 preset 对应的模式。取不到时 **fail-closed 到 audit**（不注册写工具）：
 *  宁可让写卡模式明显缺工具（一眼看得出、好修），也不能在体检模式悄悄留下写云入口。 */
function resolveMode(ctx) {
  let presetId;
  try {
    presetId = ctx.agentPresets && ctx.agentPresets.composedPreset
      ? ctx.agentPresets.composedPreset(ctx)
      : undefined;
  } catch (error) {
    console.error(`[flomo] composedPreset 调用失败：${error && error.message}`);
  }
  const mode = MODE_OF_PRESET[presetId];
  if (mode) return { mode, known: true };
  console.error(`[flomo] 无法确定所在 preset（composedPreset=${String(presetId)}），按 audit 处理：不注册写工具`);
  return { mode: 'audit', known: false };
}

// ---------------------------------------------------------------- 工具注册

/** 本插件使用的参数 spec →  enforced JSON Schema 子集。 */
function toJsonSchema(spec) {
  const properties = {};
  const required = [];
  for (const [key, value] of Object.entries(spec || {})) {
    const property = { type: value.type };
    if (value.description) property.description = value.description;
    if (value.enum) property.enum = value.enum;
    if (value.items) property.items = value.items;
    properties[key] = property;
    if (value.required) required.push(key);
  }
  return { type: 'object', properties, required, additionalProperties: false };
}

/** 注册一个工具，附带一条形态试探梯子（见文件头说明）。 */
function registerTool(ctx, def) {
  const attempts = [
    { label: 'json-schema', parameters: toJsonSchema(def.parameters) },
    { label: 'spec', parameters: def.parameters },
  ];
  for (const attempt of attempts) {
    for (const withPresent of [true, false]) {
      const definition = {
        name: def.name,
        description: def.description,
        parameters: attempt.parameters,
        output: {},
        execute: def.execute,
      };
      if (withPresent && def.presentCall) definition.presentCall = def.presentCall;
      try {
        ctx.tools.register(definition);
        console.log(`[flomo] 工具 ${def.name} 已注册（形态 ${attempt.label}${withPresent ? '+presentCall' : ''}）`);
        return true;
      } catch (error) {
        console.error(`[flomo] 工具 ${def.name} 形态 ${attempt.label}${withPresent ? '+presentCall' : ''} 被拒：${error && error.message}`);
      }
    }
  }
  console.error(`[flomo] 工具 ${def.name} 所有形态均被拒，本次未注册`);
  return false;
}

/** 统一的「解析环境 → 跑脚本 → 回传」骨架。 */
async function runRepoScript(ctx, exec, script, args) {
  const root = repoRootOf(ctx, exec);
  if (!root) throw new Error('找不到 flomo-note 仓库根：当前会话工作目录下没有 scripts/flomo_client.py');
  const python = pythonBin();
  if (!python) throw new Error('找不到可用的 Python 解释器（试过 FLOMO_PYTHON_BIN、dsh 运行时内置、PATH 上的 python/python3）');
  return runScript(python, root, script, args);
}

// ---------------------------------------------------------------- 五个工具

function readTool(ctx) {
  return {
    name: 'flomo_read',
    kind: 'read',
    description:
      '单次 flomo 云端只读查询（一次调用 = 一次 scripts/flomo_client.py 执行）。'
      + '查重两路（关键词 + tag_tree）必须分两次调用并分别读返回，本工具不提供批量。'
      + 'batch_get 永远返回全文，不得用截断快照判断正文。',
    parameters: {
      action: {
        type: 'string', required: true,
        enum: ['search', 'batch_get', 'recommended', 'tag_tree', 'daily_review', 'format_guide', 'tag_guide'],
        description: '要跑的只读动作',
      },
      keywords: { type: 'string', description: 'search 用：短核心词（整行概念名含全角标点时常返 0）' },
      tag: { type: 'string', description: 'search 用：按顶层或子标签精确检索' },
      ids: { type: 'array', items: { type: 'string' }, description: 'batch_get 用：memo id 列表，单次不超过 10 个' },
      id: { type: 'string', description: 'recommended 用：锚定 memo 的 id' },
      limit: { type: 'integer', description: '返回条数上限；tag_tree 默认 2000 取全量' },
    },
    presentCall: (args) => ({ card: 'generic', title: `flomo 读取 ${args && args.action}`, kind: 'read' }),
    execute: async (args, exec) => {
      const spec = READ_SPECS[args.action];
      if (!spec) throw new Error(`未知 action：${args.action}`);
      const toolArgs = Object.assign({}, spec.defaults);
      for (const field of spec.fields) {
        const value = args[field];
        if (value !== undefined && value !== null && value !== '') toolArgs[field] = value;
      }
      const root = repoRootOf(ctx, exec);
      if (!root) throw new Error('找不到 flomo-note 仓库根：当前会话工作目录下没有 scripts/flomo_client.py');
      const argFile = writeTemp(root, 'flomo_req', JSON.stringify(toolArgs, null, 2));
      const argv = [spec.tool, '--file', argFile];
      if (spec.brief) argv.push('--brief');
      const result = await runRepoScript(ctx, exec, 'flomo_client.py', argv);
      return { action: args.action, cloud_tool: spec.tool, ...result };
    },
  };
}

function scanTool(ctx) {
  return {
    name: 'flomo_scan',
    kind: 'read',
    description:
      '跑仓库里的只读取证脚本（不写云端、不改任何卡）：'
      + 'qualified_concepts 统计概念名里的切片限定；concept_duplication 找归一化后同名的平行卡簇。',
    parameters: {
      action: {
        type: 'string', required: true,
        enum: ['qualified_concepts', 'concept_duplication'],
        description: '跑哪个取证脚本',
      },
      tags: {
        type: 'array', items: { type: 'string' },
        description: '要扫描的标签（建议二级，如 科技/安全）；省略则按脚本默认取样范围',
      },
    },
    presentCall: (args) => ({ card: 'generic', title: `flomo 扫描 ${args && args.action}`, kind: 'read' }),
    execute: async (args, exec) => {
      const script = SCAN_SCRIPTS[args.action];
      if (!script) throw new Error(`未知 action：${args.action}`);
      const tags = Array.isArray(args.tags)
        ? args.tags.filter((tag) => typeof tag === 'string' && tag.trim() !== '')
        : [];
      const result = await runRepoScript(ctx, exec, script, tags);
      return { action: args.action, script, ...result };
    },
  };
}

function gateTool(ctx) {
  return {
    name: 'flomo_gate',
    kind: 'read',
    description:
      '跑 SOP 流程闸门：代跑查重两路、校验第 2 步验证留痕与其正文落点，全过才落凭证（6 小时有效）。'
      + '写云前必须先过本工具；退出码非 0 即未过，未过不得写云。',
    parameters: {
      content: { type: 'string', required: true, description: '待写卡的完整正文（首行标签段 + 空行 + 正文）' },
      verify: {
        type: 'string',
        description: '第 2 步验证记录的 JSON 字符串，形如 {"searched":true,"terms":[…]}；确认非术语卡时可省略并设 skip_web',
      },
      skip_web: { type: 'boolean', description: '仅当卡片主体不是专业术语/机构/产品/模型/定理简称时使用，会在凭证留痕' },
      anchor_id: { type: 'string', description: '可选：本卡 id，更新场景下作复盘锚点' },
    },
    presentCall: () => ({ card: 'generic', title: 'flomo 流程闸门', kind: 'read' }),
    execute: async (args, exec) => {
      const root = repoRootOf(ctx, exec);
      if (!root) throw new Error('找不到 flomo-note 仓库根：当前会话工作目录下没有 scripts/flomo_client.py');
      const python = pythonBin();
      if (!python) throw new Error('找不到可用的 Python 解释器');
      const memoFile = writeTemp(root, 'memo_body', args.content);
      const argv = ['--memo', memoFile];
      if (args.skip_web === true) {
        argv.push('--skip-web');
      } else {
        if (!args.verify) {
          throw new Error('术语卡必须传 verify（第 2 步验证记录 JSON 字符串）；确认非术语卡才可设 skip_web=true');
        }
        let parsed;
        try {
          parsed = JSON.parse(args.verify);
        } catch (error) {
          throw new Error(`verify 不是合法 JSON：${error && error.message}`);
        }
        argv.push('--verify', writeTemp(root, 'flomo_verify', JSON.stringify(parsed, null, 2)));
      }
      if (args.anchor_id) argv.push('--anchor-id', String(args.anchor_id));
      const result = await runScript(python, root, 'sop_gate.py', argv);
      return { gate: 'sop_gate', draft: memoFile, ...result };
    },
  };
}

function validateTool(ctx) {
  return {
    name: 'flomo_validate',
    kind: 'read',
    description:
      '文本质检：格式硬限 + 流程闸门凭证（存在、签名相符、正文指纹相符、未过期）。'
      + '返回 ERR/WARN 与退出码；有 ERR 即不得写云。',
    parameters: {
      content: { type: 'string', required: true, description: '待校验的完整卡片正文' },
    },
    presentCall: () => ({ card: 'generic', title: 'flomo 文本质检', kind: 'read' }),
    execute: async (args, exec) => {
      const root = repoRootOf(ctx, exec);
      if (!root) throw new Error('找不到 flomo-note 仓库根：当前会话工作目录下没有 scripts/flomo_client.py');
      const draft = writeTemp(root, 'memo_body', args.content);
      const result = await runRepoScript(ctx, exec, 'validate_memo.py', ['--file', draft]);
      return { validator: 'validate_memo', draft, ...result };
    },
  };
}

function writeTool(ctx, modeKnown) {
  return {
    name: 'flomo_write',
    kind: 'write',
    description:
      '写云端：create 新建卡片 / update 覆盖式更新 / rename_tag 全库重命名标签。'
      + '一次调用 = 一次脚本执行；脚本内置写前幂等查重与写后回读全文逐字验收。'
      + 'create/update 会先跑 validate_memo（格式 + 闸门凭证），不过则一个字节都不发云端。'
      + 'update 前须先 flomo_read(batch_get) 回读原卡全文；rename_tag 前须先 tag_tree 现采确认旧标签存在。',
    parameters: {
      action: {
        type: 'string', required: true,
        enum: ['create', 'update', 'rename_tag'],
        description: '写动作',
      },
      content: { type: 'string', description: 'create/update 用：完整卡片正文' },
      id: { type: 'string', description: 'update 用：目标 memo id，必须来自查询结果、不得手抄' },
      old_tag: { type: 'string', description: 'rename_tag 用：原标签名（含顶层斜杠）' },
      new_tag: { type: 'string', description: 'rename_tag 用：新标签名' },
    },
    presentCall: (args) => ({ card: 'generic', title: `flomo 写入 ${args && args.action}`, kind: 'other' }),
    execute: async (args, exec) => {
      // apply 时没能确定模式的场合，这里再查一次并 fail-closed。
      if (!modeKnown && resolveMode(ctx).mode === 'audit') {
        throw new Error('体检模式不提供写云入口；确需写云请先 /flomo write 切换模式');
      }
      const root = repoRootOf(ctx, exec);
      if (!root) throw new Error('找不到 flomo-note 仓库根：当前会话工作目录下没有 scripts/flomo_client.py');
      const python = pythonBin();
      if (!python) throw new Error('找不到可用的 Python 解释器');

      if (args.action === 'rename_tag') {
        if (!args.old_tag || !args.new_tag) throw new Error('rename_tag 需要 old_tag 与 new_tag');
        const argFile = writeTemp(root, 'flomo_req', JSON.stringify(
          { old_tag: args.old_tag, new_tag: args.new_tag }, null, 2,
        ));
        const result = await runScript(python, root, 'flomo_client.py', ['tag_rename', '--file', argFile]);
        return { action: 'rename_tag', ...result };
      }

      if (!args.content || String(args.content).trim() === '') {
        throw new Error(`${args.action} 需要 content（完整卡片正文）`);
      }
      if (args.action === 'update' && !args.id) {
        throw new Error('update 需要 id（目标 memo id，须来自查询结果，不得手抄）');
      }

      // 第一道：文本质检（含闸门凭证）。不过则**不启动** flomo_client，云端零调用。
      const draft = writeTemp(root, 'memo_body', args.content);
      const gate = await runScript(python, root, 'validate_memo.py', ['--file', draft]);
      if (gate.code !== 0) {
        return {
          action: args.action,
          blocked: 'validate_memo',
          validate: gate,
          hint: '文本质检未过（格式 ERR 或闸门凭证缺失/不匹配），未向云端发送任何内容；先修正文，或先跑 flomo_gate 出凭证。',
        };
      }

      const payload = args.action === 'create'
        ? { content: args.content }
        : { id: args.id, content: args.content };
      const argFile = writeTemp(root, 'flomo_req', JSON.stringify(payload, null, 2));
      const cloudTool = args.action === 'create' ? 'memo_create' : 'memo_update';
      const result = await runScript(python, root, 'flomo_client.py', [cloudTool, '--file', argFile]);
      return { action: args.action, cloud_tool: cloudTool, gate: { code: gate.code }, ...result };
    },
  };
}

// ---------------------------------------------------------------- 插件入口

export function apply(ctx) {
  const resolved = resolveMode(ctx);
  const mode = resolved.mode;

  registerTool(ctx, readTool(ctx));
  registerTool(ctx, scanTool(ctx));
  if (mode !== 'audit') {
    registerTool(ctx, gateTool(ctx));
    registerTool(ctx, validateTool(ctx));
    registerTool(ctx, writeTool(ctx, resolved.known));
  } else {
    console.log('[flomo] 体检模式：只注册 flomo_read / flomo_scan，不注册写工具');
  }

  // /flomo <模式>：切会话 preset（product 自己的模式切换入口，与hero 控件同一条路）。
  ctx.commands.register({
    name: 'flomo',
    description: '切换 flomo 工作台模式：write（写卡）/ tidy（治理）/ audit（体检）',
    input: { hint: '<write|tidy|audit>' },
    handler: async ({ agent, rawInput }) => {
      const wanted = String(rawInput == null ? '' : rawInput).trim();
      const preset = PRESET_OF_MODE[wanted];
      if (!preset) {
        return { kind: 'error', text: '用法：/flomo write | tidy | audit' };
      }
      try {
        await ctx.agentPresets.select(agent, preset);
        return { kind: 'success', text: `已切换到 ${preset} 预设，新模式在下一步生效。` };
      } catch (error) {
        return {
          kind: 'error',
          text: `切换失败：${error && error.message ? error.message : error}。可在新会话的预设控件里选 ${preset}。`,
        };
      }
    },
  });

  console.log(`[flomo] flomo-workbench 已在 ${mode} 模式挂载`);
}
