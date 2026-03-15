#!/bin/bash
set -euo pipefail

# ============================================================
# ex-logger setup script
#
# - data/ 以下にデータ保存用ディレクトリを作成
# - /exp-logger にこのディレクトリへのシンボリックリンクを作成
# - udev-rules/ 以下のルールファイルをインストール
# - *.service.in テンプレートからサービスファイルを生成・インストール
# - systemctl daemon-reload を実行
#
# Usage: sudo bash setup.sh [--basedir /path/to/dir]
# ============================================================

# --- 引数解析 ---
BASEDIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --basedir)
            BASEDIR="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: sudo bash $0 [--basedir /path/to/dir]"
            echo ""
            echo "Options:"
            echo "  --basedir DIR   作業用ディレクトリを指定 (デフォルト: このスクリプトの場所)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# デフォルト: このスクリプトの置かれているディレクトリ
if [[ -z "$BASEDIR" ]]; then
    BASEDIR="$(cd "$(dirname "$0")" && pwd)"
fi

SYMLINK="/exp-logger"
DATADIR="${BASEDIR}/data"
SERVICEDIR="/etc/systemd/system"
UDEVDIR="/etc/udev/rules.d"

echo "========================================"
echo " ex-logger setup"
echo "========================================"
echo "Base directory : ${BASEDIR}"
echo "Symlink        : ${SYMLINK} -> ${BASEDIR}"
echo "Data directory : ${DATADIR}"
echo "----------------------------------------"

# --- root チェック ---
if [[ $EUID -ne 0 ]]; then
    echo "Error: このスクリプトは root (sudo) で実行してください" >&2
    exit 1
fi

# --- サービスユーザーの決定 ---
SVC_USER="${SUDO_USER:-$(whoami)}"
SVC_HOME="$(eval echo "~${SVC_USER}")"

# --- コンフィグファイルの読み込み ---
CONFIGFILE="${BASEDIR}/config.conf"
if [[ ! -f "$CONFIGFILE" ]]; then
    echo "Error: コンフィグファイルが見つかりません: ${CONFIGFILE}" >&2
    exit 1
fi
# shellcheck source=config.conf
source "$CONFIGFILE"
echo "Config              : ${CONFIGFILE}"
echo "  GPS_BAUDRATE      : ${GPS_BAUDRATE:?GPS_BAUDRATE が config.conf に未設定です}"
echo "  ENABLE_GPS_TIME_SYNC : ${ENABLE_GPS_TIME_SYNC:-false}"
echo "  ENABLE_PPS        : ${ENABLE_PPS:-false}"
if [[ "${ENABLE_PPS:-false}" == "true" ]]; then
    echo "  PPS_GPIO_PIN      : ${PPS_GPIO_PIN:?PPS_GPIO_PIN が config.conf に未設定です}"
fi
echo "  ENABLE_NETMON     : ${ENABLE_NETMON:-false}"
if [[ "${ENABLE_NETMON:-false}" == "true" ]]; then
    echo "  NETMON_INTERFACES : ${NETMON_INTERFACES:?NETMON_INTERFACES が config.conf に未設定です}"
fi
echo "  ENABLE_IPERF3_SRV : ${ENABLE_IPERF3_SRV:-false}"
if [[ "${ENABLE_IPERF3_SRV:-false}" == "true" ]]; then
    echo "  IPERF3_SRV_PORT   : ${IPERF3_SRV_PORT:?IPERF3_SRV_PORT が config.conf に未設定です}"
fi
echo "  ENABLE_EXPLOGGER_CLT : ${ENABLE_EXPLOGGER_CLT:-false}"
echo "----------------------------------------"

# --- パッケージのインストール ---
echo "[1/10] 依存パッケージをインストール..."
PACKAGES=(gpsd gpsd-clients chrony)
if [[ "${ENABLE_PPS:-false}" == "true" ]]; then
    PACKAGES+=(pps-tools)
fi
if [[ "${ENABLE_IPERF3_SRV:-false}" == "true" ]]; then
    PACKAGES+=(iperf3)
fi
apt-get update -qq
apt-get install -y -qq "${PACKAGES[@]}"
echo "  -> ${PACKAGES[*]} をインストール済み"

# --- systemd-timesyncd の停止・無効化と chrony の有効化 ---
echo "[2/10] 時刻同期サービスを設定..."
if systemctl is-active --quiet systemd-timesyncd 2>/dev/null; then
    systemctl stop systemd-timesyncd
    echo "  -> systemd-timesyncd を停止"
fi
if systemctl is-enabled --quiet systemd-timesyncd 2>/dev/null; then
    systemctl disable systemd-timesyncd
    echo "  -> systemd-timesyncd を無効化"
fi
systemctl enable chrony
systemctl start chrony
echo "  -> chrony を有効化・起動"

# --- GPS 時刻同期 (chrony refclock SHM) ---
echo "[3/10] chrony GPS 時刻同期を設定..."
CHRONY_CONF="/etc/chrony/chrony.conf"
if [[ "${ENABLE_GPS_TIME_SYNC:-false}" == "true" ]]; then
    if grep -q 'refid GPS' "$CHRONY_CONF" 2>/dev/null; then
        echo "  -> refclock SHM (refid GPS) は既に設定済み (スキップ)"
    else
        if [[ "${ENABLE_PPS:-false}" == "true" ]]; then
            # PPS有効時: GPS(NMEA)は秒の特定のみに使い、時刻選択からは除外
            cat >> "$CHRONY_CONF" <<CHRONYEOF

# --- ex-logger setup.sh により追記 (GPS via gpsd SHM) ---
refclock SHM 0 refid GPS precision 1e-1 offset 0.0 delay 0.2 noselect
CHRONYEOF
            echo "  -> refclock SHM 0 refid GPS (noselect) を追記"
        else
            # PPS無効時: GPS(NMEA)を優先時刻源として使用
            cat >> "$CHRONY_CONF" <<CHRONYEOF

# --- ex-logger setup.sh により追記 (GPS via gpsd SHM) ---
refclock SHM 0 refid GPS precision 1e-1 offset 0.0 delay 0.2 prefer trust
CHRONYEOF
            echo "  -> refclock SHM 0 refid GPS (prefer trust) を追記"
        fi
    fi
else
    echo "  -> ENABLE_GPS_TIME_SYNC=false (スキップ)"
fi

# --- PPS 時刻同期 ---
echo "[4/10] PPS 時刻同期を設定..."
if [[ "${ENABLE_PPS:-false}" == "true" ]]; then
    # chrony に PPS refclock を追加
    if grep -q 'refid PPS' "$CHRONY_CONF" 2>/dev/null; then
        echo "  -> refclock PPS (refid PPS) は既に設定済み (スキップ)"
    else
        cat >> "$CHRONY_CONF" <<CHRONYEOF

# --- ex-logger setup.sh により追記 (PPS) ---
refclock PPS /dev/pps0 refid PPS precision 1e-7 lock GPS prefer
CHRONYEOF
        echo "  -> refclock PPS /dev/pps0 refid PPS precision 1e-7 lock GPS prefer を追記"
    fi

    # Raspberry Pi の場合、config.txt に dtoverlay を追加
    BOOT_CONFIG="/boot/firmware/config.txt"
    if [[ -f "$BOOT_CONFIG" ]]; then
        PPS_OVERLAY="dtoverlay=pps-gpio,gpiopin=${PPS_GPIO_PIN}"
        if grep -q "^${PPS_OVERLAY}$" "$BOOT_CONFIG" 2>/dev/null; then
            echo "  -> ${PPS_OVERLAY} は既に config.txt に設定済み (スキップ)"
        else
            # 最後の [all] セクションに追記
            # [all] が複数ある場合、最後のものを使う
            LAST_ALL_LINE="$(grep -n '^\[all\]' "$BOOT_CONFIG" | tail -1 | cut -d: -f1)"
            if [[ -n "$LAST_ALL_LINE" ]]; then
                sed -i "${LAST_ALL_LINE}a\\${PPS_OVERLAY}" "$BOOT_CONFIG"
                echo "  -> ${BOOT_CONFIG} の [all] セクションに ${PPS_OVERLAY} を追加"
            else
                echo "  警告: ${BOOT_CONFIG} に [all] セクションが見つかりません" >&2
                echo "${PPS_OVERLAY}" >> "$BOOT_CONFIG"
                echo "  -> ${BOOT_CONFIG} の末尾に ${PPS_OVERLAY} を追記"
            fi
            echo "  *** PPS の dtoverlay 変更を反映するには再起動が必要です ***"
        fi
    else
        echo "  -> ${BOOT_CONFIG} が見つかりません (Raspberry Pi 以外の環境?)"
    fi
else
    echo "  -> ENABLE_PPS=false (スキップ)"
fi

# chrony を再起動して設定を反映
systemctl restart chrony
echo "  -> chrony を再起動"

# --- /etc/default/gpsd の書き換え ---
echo "[5/10] /etc/default/gpsd を設定..."
GPSD_DEFAULT="/etc/default/gpsd"
if [[ -f "$GPSD_DEFAULT" ]]; then
    # 既存の DEVICES= 行をコメントアウト
    sed -i 's/^DEVICES=/#&/' "$GPSD_DEFAULT"
    # 既存の GPSD_OPTIONS= 行をコメントアウト
    sed -i 's/^GPSD_OPTIONS=/#&/' "$GPSD_DEFAULT"
    echo "  -> 既存の DEVICES / GPSD_OPTIONS 行をコメントアウト"
fi
# DEVICES を組み立て
GPSD_DEVICES="/dev/ttyGPS"
if [[ "${ENABLE_PPS:-false}" == "true" ]]; then
    GPSD_DEVICES="/dev/ttyGPS /dev/pps0"
fi
# 新しい設定を追記
cat >> "$GPSD_DEFAULT" <<GPSDEOF

# --- ex-logger setup.sh により追記 ---
DEVICES="${GPSD_DEVICES}"
GPSD_OPTIONS="-n -s ${GPS_BAUDRATE}"
GPSDEOF
echo "  -> DEVICES=\"${GPSD_DEVICES}\""
echo "  -> GPSD_OPTIONS=\"-n -s ${GPS_BAUDRATE}\""

systemctl enable gpsd
systemctl restart gpsd
echo "  -> gpsd を有効化・再起動"

# --- udev ルールのインストール ---
echo "[6/10] udev ルールをインストール..."
if [[ -d "${BASEDIR}/udev-rules" ]]; then
    for rules_file in "${BASEDIR}"/udev-rules/*.rules; do
        if [[ ! -f "$rules_file" ]]; then
            continue
        fi
        rules_name="$(basename "$rules_file")"
        cp "$rules_file" "${UDEVDIR}/${rules_name}"
        echo "  -> ${rules_file} => ${UDEVDIR}/${rules_name}"
    done
    udevadm control --reload-rules
    udevadm trigger
    echo "  -> udev ルールをリロード"
else
    echo "  警告: ${BASEDIR}/udev-rules/ が見つかりません (スキップ)" >&2
fi

# --- データディレクトリ作成 ---
echo "[7/10] データディレクトリを作成..."
mkdir -p "${DATADIR}/gps"
echo "  -> ${DATADIR}/gps/"
if [[ "${ENABLE_NETMON:-false}" == "true" ]]; then
    mkdir -p "${DATADIR}/netmon"
    echo "  -> ${DATADIR}/netmon/"
fi
if [[ "${ENABLE_IPERF3_SRV:-false}" == "true" ]]; then
    mkdir -p "${DATADIR}/iperf3-srv"
    echo "  -> ${DATADIR}/iperf3-srv/"
fi
if [[ -n "${SUDO_USER:-}" ]]; then
    chown -R "${SUDO_USER}:${SUDO_USER}" "${DATADIR}"
fi

# --- シンボリックリンク作成 ---
echo "[8/10] シンボリックリンクを作成..."
if [[ -L "$SYMLINK" ]]; then
    CURRENT_TARGET="$(readlink -f "$SYMLINK")"
    if [[ "$CURRENT_TARGET" == "$BASEDIR" ]]; then
        echo "  -> ${SYMLINK} は既に ${BASEDIR} を指しています (スキップ)"
    else
        echo "  -> ${SYMLINK} の指す先を ${CURRENT_TARGET} から ${BASEDIR} に変更"
        ln -sfn "${BASEDIR}" "${SYMLINK}"
    fi
elif [[ -e "$SYMLINK" ]]; then
    echo "Error: ${SYMLINK} が既に存在し、シンボリックリンクではありません" >&2
    echo "  手動で確認・削除してから再実行してください" >&2
    exit 1
else
    ln -s "${BASEDIR}" "${SYMLINK}"
    echo "  -> ${SYMLINK} -> ${BASEDIR}"
fi

# --- テンプレートからサービスファイルを生成・インストール ---
echo "[9/11] テンプレートからサービスファイルを生成・インストール..."
INSTALLED_SERVICES=()
for template in "${BASEDIR}"/services/*.service.in; do
    if [[ ! -f "$template" ]]; then
        echo "  警告: テンプレートファイルが見つかりません" >&2
        continue
    fi

    # foo.service.in -> foo.service
    service_name="$(basename "$template" .in)"

    # 無効なサービスはスキップ
    if [[ "$service_name" == "netmon.service" && "${ENABLE_NETMON:-false}" != "true" ]]; then
        echo "  -> ${service_name} はスキップ (ENABLE_NETMON=false)"
        continue
    fi
    if [[ "$service_name" == "explogger-clt.service" && "${ENABLE_EXPLOGGER_CLT:-false}" != "true" ]]; then
        echo "  -> ${service_name} はスキップ (ENABLE_EXPLOGGER_CLT=false)"
        continue
    fi
    if [[ "$service_name" == "iperf3-srv.service" && "${ENABLE_IPERF3_SRV:-false}" != "true" ]]; then
        echo "  -> ${service_name} はスキップ (ENABLE_IPERF3_SRV=false)"
        continue
    fi

    sed \
        -e "s|@@SYMLINK@@|${SYMLINK}|g" \
        -e "s|@@SVC_USER@@|${SVC_USER}|g" \
        -e "s|@@SVC_HOME@@|${SVC_HOME}|g" \
        -e "s|@@NETMON_INTERFACES@@|${NETMON_INTERFACES:-eth0}|g" \
        -e "s|@@IPERF3_SRV_PORT@@|${IPERF3_SRV_PORT:-5201}|g" \
        "$template" > "${SERVICEDIR}/${service_name}"

    INSTALLED_SERVICES+=("$service_name")
    echo "  -> ${template} => ${SERVICEDIR}/${service_name}"
done

# --- daemon-reload ---
echo "[10/11] systemd daemon-reload..."
systemctl daemon-reload
echo "  -> done"

# --- サービスの有効化・起動 ---
echo "[11/11] サービスを有効化・起動..."
for svc in "${INSTALLED_SERVICES[@]}"; do
    systemctl enable "$svc"
    systemctl restart "$svc"
    echo "  -> ${svc} を enable & start"
done

echo ""
echo "========================================"
echo " セットアップ完了"
echo "========================================"
echo ""
echo "データ保存先:"
echo "  GPS   : ${SYMLINK}/data/gps/"
if [[ "${ENABLE_NETMON:-false}" == "true" ]]; then
    echo "  Netmon: ${SYMLINK}/data/netmon/"
fi
if [[ "${ENABLE_IPERF3_SRV:-false}" == "true" ]]; then
    echo "  iperf3: ${SYMLINK}/data/iperf3-srv/"
fi
echo ""
echo "注: ${SYMLINK} は ${BASEDIR} へのシンボリックリンクです。"
echo "    ディレクトリを移動した場合は再度 setup.sh を実行してください。"
