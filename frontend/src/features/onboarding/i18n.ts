export const en = {
  onboarding: {
    ov: {
      inUse: 'In use',
      tryHere: 'Try it here',
      useKey: 'Use your key “{name}” ({prefix}…) as the API key.',
    },
    // "needs your action" (admins): one line above the slot
    action: {
      lead: 'Waiting for you:',
      bookings: 'no bookings to approve | 1 booking to approve | {n} bookings to approve',
      signups: 'no sign-ups to approve | 1 sign-up to approve | {n} sign-ups to approve',
      errors: 'no errors | 1 system error | {n} system errors',
    },
    // the slot's other cards, folded into links
    also: {
      work: 'My work ({n})',
      setup: 'Setup: {n} left',
      start: 'Start here',
    },
    work: {
      title: 'My work',
      subtitle: 'Running now or starting within 24 hours.',
      env: 'Dev environment',
      exclusive: 'Whole GPU',
      left: '{time} left',
      startsIn: 'starts {when}',
      pending: 'awaiting approval',
      starting: 'starting…',
      extend: '+1 h',
      extended: 'Extended by an hour.',
      open: 'Open',
      copySsh: 'Copy ssh',
      sshCopied: 'SSH command copied.',
      all: 'All environments →',
      schedule: 'Schedule →',
    },
    setup: {
      title: 'Finish setting up SlotServe',
      subtitle: '{n} step left | {n} steps left',
      continue: 'Continue setup',
      progress: '{done} of {total} done',
    },
    start: {
      title: 'Welcome, {name}. Here you can:',
      dismiss: 'Don’t show again',
      done: 'Done',
      progress: '{done} of {total} done',
      call: {
        title: 'Call a model',
        nowModel: 'Ready now: {model}',
        now: 'No booking needed · answers in seconds',
        later: 'Book a slot and the model is loaded for you',
        action: 'Try it here',
      },
      env: {
        title: 'Open a dev environment',
        desc: 'VS Code or Jupyter in your browser',
        hint: 'No GPU needed · ready in about a minute',
        action: 'Dev environments',
      },
      book: {
        title: 'Book the whole GPU',
        desc: 'For training or anything that needs the GPU alone',
        hint: 'See who is using it, then book an hour',
        action: 'Book the GPU',
      },
    },
  },
  auth: {
    demoQueued: 'queued: {model} loads for {user}',
    demoEnv: 'VS Code in the browser',
    demoEnvNote: 'dev environment · no GPU',
  },
}

export const zh: typeof en = {
  onboarding: {
    ov: {
      inUse: '使用中',
      tryHere: '在这里试试',
      useKey: '把你的密钥「{name}」（{prefix}…）作为 API 密钥。',
    },
    action: {
      lead: '等你处理：',
      bookings: '没有待审批预约 | 1 个预约待审批 | {n} 个预约待审批',
      signups: '没有待批准注册 | 1 个注册待批准 | {n} 个注册待批准',
      errors: '没有错误 | 1 个系统错误 | {n} 个系统错误',
    },
    also: {
      work: '我的工作（{n}）',
      setup: '设置还差 {n} 步',
      start: '从这里开始',
    },
    work: {
      title: '我的工作',
      subtitle: '正在运行或 24 小时内开始。',
      env: '开发环境',
      exclusive: '整块 GPU',
      left: '剩余 {time}',
      startsIn: '{when}开始',
      pending: '等待审批',
      starting: '启动中…',
      extend: '+1 小时',
      extended: '已延长 1 小时。',
      open: '打开',
      copySsh: '复制 ssh',
      sshCopied: '已复制 SSH 命令。',
      all: '全部环境 →',
      schedule: '预约 →',
    },
    setup: {
      title: '完成 SlotServe 的设置',
      subtitle: '还差 {n} 步 | 还差 {n} 步',
      continue: '继续设置',
      progress: '已完成 {done}/{total}',
    },
    start: {
      title: '欢迎，{name}。在这里你可以：',
      dismiss: '不再显示',
      done: '已完成',
      progress: '已完成 {done}/{total}',
      call: {
        title: '调用大模型',
        nowModel: '现在就能用：{model}',
        now: '无需预约 · 几秒内回复',
        later: '预约一个时段，系统会为你加载模型',
        action: '在这里试试',
      },
      env: {
        title: '打开开发环境',
        desc: '浏览器里的 VS Code 或 Jupyter',
        hint: '无需 GPU · 大约一分钟就绪',
        action: '开发环境',
      },
      book: {
        title: '预约整块 GPU',
        desc: '训练或需要独占显卡的任务',
        hint: '先看看谁在用，再预约 1 小时',
        action: '预约 GPU',
      },
    },
  },
  auth: {
    demoQueued: '排队中：正为 {user} 加载 {model}',
    demoEnv: '浏览器里的 VS Code',
    demoEnvNote: '开发环境 · 无需 GPU',
  },
}
