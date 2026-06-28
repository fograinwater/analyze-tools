#!/bin/bash
# 脚本作用：使用filebench测试文件系统

# sudo rm -rf /tmp/filebench-shm-*
# ================================
# 参数设置
# ================================

# BASE: zlfs_on_zns | f2fs_on_ssdzns | ext4_on_ssd | cachefs_on_zlfs | cachefs_on_ext4 | cachefs_on_zlfs_medfs | medfs | olk6.6
BASE="medfs"
DEVICE_ZNS=/dev/nvme1n1
DEVICE_SSD_CACHE=/dev/nvme3n1p1
DEVICE_MEDFS=/dev/mapper/delaydisk
DEVICE_MEDFS=/dev/nvme2n1
# MODE: rand_read_30 | rand_read_30-1MB-50G | rand_read_30-1MB-1TB | rand_read_30-64KB-50GB
# RUN_MODE="rand_read_30-1MB-1TB"
RUN_MODE="rand_read_30-1MB-1TB"
# RUN_MODE="rand_read_30"
# RUN_MODE="seq_read_30"
# RUN_MODE="seq_30_bigfile"
# RUN_MODE="rand_write_30"

# ================================
# 根据上述参数设定而自动产生相应的负载日志文件
# ================================
WORKLOAD=/home/ttt/filebench-use-case/workloads_out/${BASE}/${RUN_MODE}.f
LOG_DIR=/home/ttt/filebench-use-case/filebenchRunLog
mkdir -p "$LOG_DIR"

TS=$(date +"%Y%m%d-%H%M%S")
FILEBENCH_LOG=${LOG_DIR}/filebench_run_log_${BASE}_${RUN_MODE}_${TS}.log
IOSTAT_LOG_ZNS=${LOG_DIR}/iostat_${BASE}_${RUN_MODE}_ZNS_${TS}.log
IOSTAT_LOG_SSD_CACHE=${LOG_DIR}/iostat_${BASE}_${RUN_MODE}_SSD_CACHE_${TS}.log
IOSTAT_LOG_MEDFS=${LOG_DIR}/iostat_${BASE}_${RUN_MODE}_MEDFS_${TS}.log

echo "==============================================="  | sudo tee -a ${FILEBENCH_LOG}
echo "本次运行日志:"                                     | sudo tee -a ${FILEBENCH_LOG}
echo "  Filebench 日志:      $FILEBENCH_LOG"             | sudo tee -a ${FILEBENCH_LOG}
echo "  iostat    日志:"                                | sudo tee -a ${FILEBENCH_LOG}
echo "         1) ZNS:       $IOSTAT_LOG_ZNS"           | sudo tee -a ${FILEBENCH_LOG}
echo "         2) SSD_CACHE: $IOSTAT_LOG_SSD_CACHE"     | sudo tee -a ${FILEBENCH_LOG}
echo "         3) MEDFS:     $IOSTAT_LOG_MEDFS"          | sudo tee -a ${FILEBENCH_LOG}
echo "  WORKLOAD  文件:     $WORKLOAD"                  | sudo tee -a ${FILEBENCH_LOG}
echo
echo "  请运行下面的命令启动 iostat 监控设备"
echo "  sudo iostat -dxm 1 $DEVICE_ZNS | sudo tee ${IOSTAT_LOG_ZNS}"
echo "  sudo iostat -dxm 1 $DEVICE_SSD_CACHE | sudo tee ${IOSTAT_LOG_SSD_CACHE}"
echo "  sudo iostat -dxm 1 $DEVICE_MEDFS | sudo tee ${IOSTAT_LOG_MEDFS}"
echo "===============================================" 
echo

# ================================
# 运行 filebench，并将WORKLOAD内容写入日志
# ================================
cat $WORKLOAD | sudo tee -a ${FILEBENCH_LOG}
echo "===============================================" | sudo tee -a ${FILEBENCH_LOG}
echo "开始运行 filebench..." | sudo tee -a ${FILEBENCH_LOG}
echo "===============================================" | sudo tee -a ${FILEBENCH_LOG}
echo | sudo tee -a ${FILEBENCH_LOG}

echo "sudo filebench -f ${WORKLOAD} | sudo tee -a ${FILEBENCH_LOG} 2>&1"
sudo filebench -f ${WORKLOAD} | sudo tee -a ${FILEBENCH_LOG} 2>&1

echo "===============================================" | sudo tee -a ${FILEBENCH_LOG}
echo "Filebench 运行完成" | sudo tee -a ${FILEBENCH_LOG}
echo "===============================================" | sudo tee -a ${FILEBENCH_LOG}