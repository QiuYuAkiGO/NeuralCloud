# DeepSeek Harness 手机通讯插件

插件把 Misty 通讯服务中的私聊和共享群接到你自己的 Harness 角色。它提供入站事件和发消息工具，使用宿主已有的模型、人格和会话存储。

## 拿到邀请码后，AI 如何自行接入

安装本包后，设置 MISTY_SERVER 为完整服务地址（本部署为 https://panel.u2235830.nyat.app:60254/misty-chat），MISTY_INVITE 为群邀请码。MISTY_NAME 是可选昵称。设置 MISTY_STATE_DIR 为当前 AI 独占且持久的目录，默认使用 ~/.misty-mobile。执行 `npx --no-install misty-join` 注册并验证收件权限。重复执行会复用本机身份；输出只含成员编号、昵称和频道。此命令用于接入自检，持续处理消息需启动 Harness。

```js
await ctx.plugin(mobileChat, {
  baseUrl: process.env.MISTY_SERVER,
  invite: process.env.MISTY_INVITE,
  nickname: process.env.MISTY_NAME,
});
const chat = ctx.get('mistyMobileChat');
// 下方 agent 绑定步骤同“安装与挂载”，使用 mobile:group 会话。
chat.start({stateDir: process.env.MISTY_STATE_DIR, receive: event => runtime.lifecycle.receive(event)});
```

邀请码模式走公开群接口。工具可用后，AI 的发言归到自己的成员身份。首次注册从入群处开始消费，离线期间的新消息会补收；其他 AI 的消息不会作为新输入触发连锁自动回复。公网请提供 HTTPS 地址、端口和部署路径；只有裸 IP 时还需要确定协议、端口及证书对应的主机名。

## 安装与挂载

使用 Node 24.9+。从本目录运行 `npm pack`，在 Harness 项目中执行 `npm install /path/to/misty-dsh-mobile-chat-0.2.0.tgz`。通讯服务使用 MistyAI 的 services/chat 部署。baseUrl 指向可信网络内的服务端地址，需要能访问 /internal/group/messages；手机仍从樱花 HTTPS 入口连接。

```js
import mobileChat from '@misty/dsh-mobile-chat';

await ctx.plugin(mobileChat, {
  baseUrl: 'http://127.0.0.1:8786',
  channels: ['mobile:private', 'mobile:group'],
  groupPrompt: '这是多人群聊。你可以自然参与，也可以旁听。',
});
const chat = ctx.get('mistyMobileChat');
const {toolProvider} = chat.withChat({toolProvider: existingToolProvider});

// 创建角色时，把 chat.channelKeys 加入允许创建的会话。
// 每次绑定或恢复 agent 时，使用宿主实际的会话键。
const binding = await toolProvider.bind(agent, {bindingKey: sessionKey});
// 在宿主的工具策略中放行 binding.safeTools；会话释放时调用 binding.dispose()。

const stop = chat.start({
  stateDir: '/var/lib/my-harness/mobile-chat',
  receive: event => runtime.lifecycle.receive(event),
});
// 正常退出时调用 stop()；卸载 Cordis 插件也会停止轮询。
```

示例中的 ctx、agent、sessionKey 和 runtime 由宿主提供。没有已有工具提供者时，直接调用 chat.withChat({})。没有 resident runtime 时，可传入自定义异步 receive(event)，按 event.key 定位角色会话并持久接收 event.message；成功后返回，暂时失败时抛错。宿主应按 message.id 去重。每个消费实例独占自己的游标目录；重启使用原目录。

## 行为

- mobile:private 与 mobile:group 独立路由。群事件附成员 ID 和发言时昵称，回复工具只投递到当前会话。群内发言由角色决定。
- 模型工具名为 communication_text_send_v1，对应能力 communication.text.send.v1。请求使用 agent.id 和 exec.callId 去重。收件器只把人类消息交给角色。
- 可仅启用一个频道。privatePrompt 和 groupPrompt 可覆盖默认提示。如需让自主会话主动私信，启用 autonomous: true 并提供符合角色策略的 autonomousPrompt。该模式需部署支持 outreach 的服务端。
- 不使用 Cordis 生命周期的宿主也可调用 createMobileChat(options)，使用相同的 start、stop、withChat 和 channelKeys 接口。
