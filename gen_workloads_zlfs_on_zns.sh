#!/bin/bash

### 用户可修改参数 ###
DEVICE=/dev/nvme1n1
MNT=/mnt/ZNS
FILESIZE=65536            # 64KB
DIRWIDTH=100
THREADS=32
IOSIZE=16384              # 16KB
RUNTIME=300               # 300 seconds
PSRUNTIME=30

### 填充比例（百分比） ###
RATIOS=(30 50 70 90)

### I/O 模式 ###
MODES=("seq_read" "rand_read" "seq_write" "rand_write")

# ### 读取设备大小 ###
# DISK_SIZE=$(blockdev --getsize64 $DEVICE)
# echo "Detected disk size: $DISK_SIZE bytes"

# 获取文件系统当前可用容量（字节）
DISK_SIZE=$(df --output=avail -B1 $MNT | tail -1)
echo "Detected available filesystem space: $DISK_SIZE bytes"

mkdir -p workloads_out

for ratio in "${RATIOS[@]}"; do
  # 计算 fileset 字节数
  FILESET_BYTES=$(( DISK_SIZE * ratio / 100 ))
  FILECOUNT=$(( FILESET_BYTES / FILESIZE ))

  echo "[+] Ratio $ratio% -> fileset_bytes=$FILESET_BYTES filecount=$FILECOUNT"

  for mode in "${MODES[@]}"; do
    OUTFILE="zlfs_on_zns/${mode}_${ratio}.f"

    echo "  -> Generating $OUTFILE"

    ### 公共头 ###
    cat <<EOF > $OUTFILE
# Auto-generated Filebench workload

set \$dir = "$MNT"
set \$filecount = $FILECOUNT
set \$filesize = 64k

# added latency distribution profiling
enable lathist

# 设置退出模式（如果设置run time的话，可能无法写入指定的比例）
set mode quit alldone

EOF

    ### 随机模式加 gamma ###
    if [[ "$mode" == "rand_read" || "$mode" == "rand_write" ]]; then
      cat <<EOF >> $OUTFILE

set \$findex = cvar(type=cvar-gamma, min=0, max=\$filecount, parameters=mean:$(( FILECOUNT / 10 ));gamma:1.5)

EOF
    fi

    ### fileset 定义 ###
    if [[ "$mode" == "seq_write" ]]; then
      FS_NAME="write_files_new"
      cat <<EOF >> $OUTFILE
define fileset name="$FS_NAME",
    path=\$dir,
    filesize=\$filesize,
    entries=\$filecount,
    dirwidth=$DIRWIDTH,
    prealloc=0

EOF
    else
      FS_NAME="work_files"
      cat <<EOF >> $OUTFILE
define fileset name="$FS_NAME",
    path=\$dir,
    filesize=\$filesize,
    entries=\$filecount,
    dirwidth=$DIRWIDTH,
    prealloc=100,
    reuse

EOF
    fi

    ### 各模式具体定义 ###

    case $mode in

      "seq_read")
        cat <<EOF >> $OUTFILE
define process name="seq_read_${ratio}", instances=1 {
  thread name="seqread", memsize=100m, instances=$THREADS {
    flowop readwholefile name="rdwholefile", filesetname=$FS_NAME, iosize=$IOSIZE
    flowop closefile name="close"
  }
}


EOF
        ;;

      "rand_read")
        cat <<EOF >> $OUTFILE
define process name="rand_read_${ratio}", instances=1 {
  thread name="randread", memsize=100m, instances=$THREADS {
    flowop read name="rd", filesetname=$FS_NAME, iosize=$IOSIZE, indexed=\$findex
    flowop closefile name="close"
  }
}


EOF
        ;;

      "seq_write")
        cat <<EOF >> $OUTFILE

define process name="seq_write_${ratio}", instances=1 {
  thread name="seqwrite", memsize=100m, instances=$THREADS {
    flowop createfile name="ct", filesetname=$FS_NAME, fd=1
    flowop writewholefile name="wt", filesetname=$FS_NAME, iosize=$IOSIZE, fd=1
    flowop closefile name="close", fd=1
  }
}


EOF
        ;;

      "rand_write")
        cat <<EOF >> $OUTFILE

define process name="rand_write_${ratio}", instances=1 {
  thread name="randwrite", memsize=100m, instances=$THREADS {
    flowop write name="wt", filesetname=$FS_NAME, iosize=$IOSIZE, indexed=\$findex
    flowop closefile name="close"
  }
}


EOF
        ;;
    esac

    ### 公共的运行部分 ###
    cat <<EOF >> $OUTFILE
# Pre-run phase
psrun $PSRUNTIME

# Main run phase  
run $RUNTIME
EOF

  done
done

echo "All workloads generated under workloads_out/"
