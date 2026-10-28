#!/bin/sh
# mem-pro 证书自检（IP 证书只有 6.7 天有效期，必须在评测窗口内确认续期生效）
#
# 策略（每 30 分钟由 mempro-certcheck.timer 触发）：
#   1) 读本地 Caddy 证书剩余时间（不依赖网络，避免把网络抖动误判成证书问题）
#   2) 本地 HTTPS 握手自检（证书是签给 IP 的，所以直接用 IP 请求）
#   3) 剩余 < WARN_HOURS：reload caddy（Caddy 会立刻重试 ACME）
#      剩余 < CRIT_HOURS：restart caddy，并以退出码 2 结束（systemd 会标 failed，便于告警）
#      剩余 < PANIC_HOURS：额外写 CRITICAL 日志（评测期间断服风险）
#
# 可用环境变量覆盖（便于演练）：CERT_DIR / CERT_NAME / WARN_HOURS / CRIT_HOURS / PANIC_HOURS / CHECK_URL
set -u
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

CERT_DIR="${CERT_DIR:-/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/113.44.133.30}"
CERT_NAME="${CERT_NAME:-113.44.133.30.crt}"
CHECK_URL="${CHECK_URL:-https://113.44.133.30/health}"
WARN_HOURS="${WARN_HOURS:-48}"
CRIT_HOURS="${CRIT_HOURS:-24}"
PANIC_HOURS="${PANIC_HOURS:-6}"
LOG="${LOG:-/var/log/mempro-cert.log}"

log() { echo "$(date -Is) $*" | tee -a "$LOG" >&2; }
rc=0

# 刚 reload/restart 完 Caddy 会有 1–3 秒空窗，先等它能接连接再判定，避免误报
wait_https() {
    i=0
    while [ "$i" -lt 20 ]; do
        if curl -sS -o /dev/null --max-time 5 "$CHECK_URL" 2>/dev/null; then return 0; fi
        i=$((i + 1)); sleep 1
    done
    return 1
}

crt="$CERT_DIR/$CERT_NAME"
if [ ! -s "$crt" ]; then
    log "CRITICAL 找不到证书文件 $crt"
    rc=2
else
    # openssl -checkend 成功=仍有效；用不同阈值反推剩余小时数区间
    hours_left=""
    for h in "$PANIC_HOURS" "$CRIT_HOURS" "$WARN_HOURS" 168 336; do
        if openssl x509 -in "$crt" -noout -checkend $((h * 3600)) >/dev/null 2>&1; then
            hours_left="$h"
        else
            break
        fi
    done
    not_after=$(openssl x509 -in "$crt" -noout -enddate 2>/dev/null | cut -d= -f2)
    case "$hours_left" in
        336) log "OK 证书剩余 > 14 天（notAfter=$not_after）" ;;
        168) log "OK 证书剩余 > 7 天（notAfter=$not_after）" ;;
        "$WARN_HOURS") log "OK 证书剩余 < 7 天但 > ${WARN_HOURS}h（notAfter=$not_after），Caddy 应在自动续期" ;;
        "$CRIT_HOURS")
            log "WARN 证书剩余 < ${WARN_HOURS}h（notAfter=$not_after），reload caddy 触发续期"
            systemctl reload caddy || rc=1
            wait_https || log "WARN reload 后 20s 内仍连不上"
            ;;
        "$PANIC_HOURS")
            log "ERROR 证书剩余 < ${CRIT_HOURS}h（notAfter=$not_after），restart caddy"
            systemctl restart caddy || rc=2
            wait_https || log "ERROR restart 后 20s 内仍连不上"
            rc=2
            ;;
        *)
            log "CRITICAL 证书剩余 < ${PANIC_HOURS}h（notAfter=$not_after），评测期间会断服，人工介入"
            systemctl restart caddy || true
            wait_https || log "ERROR restart 后 20s 内仍连不上"
            rc=2
            ;;
    esac
fi

# 本地握手自检：只看"能不能用这张证书提供 HTTPS"，失败才 reload（避免误重载）
if ! curl -sS -o /dev/null --max-time 20 "$CHECK_URL" 2>/tmp/mempro-certcurl.err; then
    log "WARN 本地 HTTPS 自检失败（$(tr -d '\n' < /tmp/mempro-certcurl.err | cut -c1-160)），reload caddy"
    systemctl reload caddy || rc=2
    if wait_https; then
        log "OK reload 后恢复"
    else
        log "ERROR reload 后仍失败，需要人工检查 caddy / 安全组 / 备案拦截"
        rc=2
    fi
fi

exit $rc
