/**
 * OpenCode Go 本地代理
 * 问题：表单只支持 OpenAI 兼容协议，无法添加 OpenCode Go 强制要求的
 *       x-opencode-session 请求头，导致 400 MissingSessionID。
 * 解决：本代理收到表单请求后，自动补上 x-opencode-session 头，
 *       再原样转发到 https://opencode.ai。表单无需改动，只需把
 *       "接口地址" 改成 http://127.0.0.1:8787 即可。
 *
 * 用法：
 *   OPENCODE_KEY=oc_sk_xxx node opencode-go-proxy.js
 *   （也可以在表单里填 Key，代理会原样转发 Authorization 头；
 *      二选一即可，推荐在表单填，更省事）
 */

const http = require('http');
const https = require('https');

const TARGET_HOST = 'opencode.ai';
const TARGET_PORT = 443;
const LISTEN_PORT = 8787;
// 同一对话内保持稳定；新会话换个值即可（不要每次随机，否则丢失缓存收益）
const SESSION_ID = process.env.OCODE_SESSION || 'local-proxy-session-001';

const server = http.createServer((req, res) => {
  // 若设置了 OPENCODE_KEY，则注入 Authorization；否则转发表单带来的 Authorization
  const fwdHeaders = { ...req.headers, host: TARGET_HOST, 'x-opencode-session': SESSION_ID };
  if (process.env.OPENCODE_KEY && !fwdHeaders['authorization']) {
    fwdHeaders['authorization'] = 'Bearer ' + process.env.OPENCODE_KEY;
  }

  const options = {
    host: TARGET_HOST,
    port: TARGET_PORT,
    method: req.method,
    path: req.url,            // 原样透传路径，例如 /inference/go/openai/v1/chat/completions
    headers: fwdHeaders,
  };

  const upstream = https.request(options, (upRes) => {
    res.writeHead(upRes.statusCode, upRes.headers);
    upRes.pipe(res);
  });

  upstream.on('error', (e) => {
    console.error('[proxy] upstream error:', e.message);
    res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('Proxy upstream error: ' + e.message);
  });

  req.pipe(upstream);
});

server.listen(LISTEN_PORT, '127.0.0.1', () => {
  console.log(`OpenCode Go proxy listening on http://127.0.0.1:${LISTEN_PORT}`);
  console.log(`Forwarding to https://${TARGET_HOST}  (session=${SESSION_ID})`);
});
