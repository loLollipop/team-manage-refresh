## [更新] team-manage-refresh：自动踢人、续期申请、非授权清退等新功能

> 一个多月前发过一帖介绍这个项目：[\[开源分享\] 缝合式的 Team 管理及自助拉人系统：一键获取 token + 获取 JSON 文件导入 CPA](https://linux.do/t/topic/1756051)

简单更新一下这一个多月迭代的几个比较重要的功能，各位佬友可以前往项目地址查看相关说明：

- **自动踢人**：管理员可在后台开启；扫描覆盖所有兑换码（质保 + 普通无质保），到期未续期就自动从 Team 移除并销毁兑换码。普通无质保码新增"使用期限（默认 30 天）"配置。
- **续期申请**：用户可在到期前主动申请续期，管理员审批后累加时长；下一轮扫描自然按新总时长重算到期点。兑换页新增"续期申请" tab，无需等系统弹窗。
- **非授权成员清退**：能识别并自动清除成员私自拉进来的人，同时保护后台手工邀请的成员（开关启用前的存量成员永久豁免，避免误杀）。
- **CliproxyAPI 凭证推送**：一键把 Team 凭证推到 CliproxyAPI 站点（感谢 [@springandme](https://linux.do/u/springandme) 佬贡献的 PR）。
- **系统设置面板重构 + 文案精简**：相关配置归类整理，整体说明文字也做了瘦身，看起来不再那么头晕眼花。

### 关于 UI

现在整个项目的 UI 是用 codex 直接做的，不太好看，前端审美确实不太够，先在这里跟各位佬友说一声抱歉。

如果有佬友愿意提一个好看的 UI 重做 PR，我非常乐意接收！欢迎有想法的佬友参与进来 :hugs:

### 关于 Bug

新加的功能比较多，难免有没发现的 bug。如果各位佬友在使用过程中遇到任何异常或者有改进建议，欢迎到 GitHub 提 Issue 反馈，我会尽快跟进处理。

### 项目地址

:point_right: GitHub：[loLollipop/team-manage-refresh](https://github.com/loLollipop/team-manage-refresh)
:point_right: 最新发行版：[v0.2.0](https://github.com/loLollipop/team-manage-refresh/releases/tag/v0.2.0)

感谢各位佬友支持~ :hugs:
