# 安装健康看门狗（只有复制 unit 文件这一步需要 root）

SMTP 账号与收件人已写在 `data/ops/watchdog.env`（library 属主，600，不进 git；模板见 `watchdog.env.example`）。

```bash
cd /home/sjtu/paper-integrity-checker
sudo cp deploy/systemd/paper-health-watchdog.service deploy/systemd/paper-health-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now paper-health-watchdog.timer
systemctl list-timers paper-health-watchdog.timer      # 下次运行时间
sudo systemctl start paper-health-watchdog.service      # 立刻跑一次
tail data/output/ops_logs/watchdog.log
```

不装 timer 时可手工运行（读取同一个 env 文件）：

```bash
set -a; . data/ops/watchdog.env; set +a
venv/bin/python scripts/ops/health_watchdog.py --dry-run     # 只打印，不发信
venv/bin/python scripts/ops/health_watchdog.py --test-mail   # 发一封测试邮件
```

告警规则：每个问题首次出现发一封；持续存在每 REMIND_MINUTES 提醒一次；恢复后发一封「已恢复」；每天 DIGEST_HOUR 后首次运行发一封 24 小时汇总。
状态文件 `data/ops/watchdog_state.json`，日志 `data/output/ops_logs/watchdog.log`。
