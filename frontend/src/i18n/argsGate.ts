// Texts of the launch-argument policy (approval requests, admin confirmation); merged into en.ts / zh-CN.ts.

export const argsGateEn = {
  argsGate: {
    confirmTitle: 'Arguments that can run code on the server',
    confirmBody:
      'These flags can run code or write files with SlotServe’s permissions: {flags}. Use them only if you trust the model and the values.',
    confirm: 'Use them',
    requestTitle: 'Needs an admin’s approval',
    requestBody:
      'An admin has to approve these flags before the model uses them: {flags}. Until then the model keeps its current arguments. Send the request?',
    request: 'Ask an admin',
    requested: 'Sent to the admins. The model keeps its current arguments until they approve.',
    pendingBadge: 'Arguments awaiting approval',
    pendingLine: 'Awaiting approval ({flags}):',
    approve: 'Approve requested arguments',
    reject: 'Reject requested arguments',
    withdraw: 'Withdraw argument request',
    memberHint: 'Flags beyond the usual tuning options (context, GPU layers, batching…) need an admin’s approval.',
  },
  errors: {
    args_need_approval: 'An admin has to set these arguments: {flags}.',
    args_confirm_required: 'These arguments can run code or write files on the server and need a confirmation.',
    model_path_outside_library: 'Use a file or folder in the models folder (or a model library), or a Hugging Face repository id such as Qwen/Qwen3-8B.',
    custom_engine_admin_only: 'Only admins can use the custom engine.',
    no_pending_args: 'There is no argument request for this model.',
  },
}

export const argsGateZh = {
  argsGate: {
    confirmTitle: '可在服务器上执行代码的参数',
    confirmBody: '这些参数可以用 SlotServe 的权限执行代码或写文件：{flags}。只有在信任该模型和参数值时才使用。',
    confirm: '确认使用',
    requestTitle: '需要管理员批准',
    requestBody: '以下参数需管理员批准后模型才会使用：{flags}。在此之前模型保持当前参数。要提交申请吗？',
    request: '提交申请',
    requested: '已提交给管理员。批准前模型保持当前参数。',
    pendingBadge: '参数待批准',
    pendingLine: '待批准（{flags}）：',
    approve: '批准申请的参数',
    reject: '拒绝申请的参数',
    withdraw: '撤回参数申请',
    memberHint: '常用调优参数（上下文、GPU 层数、批大小等）以外的参数需管理员批准。',
  },
  errors: {
    args_need_approval: '这些参数需要由管理员设置：{flags}。',
    args_confirm_required: '这些参数可以在服务器上执行代码或写文件，需要确认。',
    model_path_outside_library: '请使用模型目录（或模型库）中的文件或文件夹，或 Hugging Face 仓库 ID（例如 Qwen/Qwen3-8B）。',
    custom_engine_admin_only: '只有管理员可以使用自定义引擎。',
    no_pending_args: '该模型没有待批准的参数申请。',
  },
}
