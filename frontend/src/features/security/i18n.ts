export const en = {
  security: {
    setupTitle: 'Set up SlotServe',
    setupIntro:
      'No admin account exists yet. Create it here with the setup code: it is printed in the server log, or run "slotserve setup-code" on the server (with Docker: docker compose exec slotserve slotserve setup-code).',
    setupCode: 'Setup code',
    setupCodePlaceholder: 'XXXX-XXXX-XXXX',
    sudoTitle: 'Confirm your password',
    sudoIntro: 'This action needs your password again (valid for 5 minutes).',
  },
  errors: {
    setup_code_required: 'Enter the setup code to create the first admin account.',
    invalid_setup_code: 'The setup code is not valid. Check the server log or run "slotserve setup-code".',
    invite_code_not_set: 'Sign-up is closed until an admin sets an invite code.',
    csrf_refused: 'The request was refused because it did not come from a SlotServe page. Reload the page and try again.',
    sudo_required: 'Enter your password again to continue.',
  },
}

export const zh: typeof en = {
  security: {
    setupTitle: '初始化 SlotServe',
    setupIntro:
      '还没有管理员账号。请在这里用初始化码创建：初始化码打印在服务器日志中，也可以在服务器上运行 "slotserve setup-code"（Docker：docker compose exec slotserve slotserve setup-code）。',
    setupCode: '初始化码',
    setupCodePlaceholder: 'XXXX-XXXX-XXXX',
    sudoTitle: '确认密码',
    sudoIntro: '此操作需要再次输入密码（5 分钟内有效）。',
  },
  errors: {
    setup_code_required: '请输入初始化码以创建第一个管理员账号。',
    invalid_setup_code: '初始化码不正确。请查看服务器日志或运行 "slotserve setup-code"。',
    invite_code_not_set: '管理员设置邀请码之前暂不开放注册。',
    csrf_refused: '请求被拒绝：它不是从 SlotServe 页面发出的。请刷新页面后重试。',
    sudo_required: '请再次输入密码以继续。',
  },
}
