#!/bin/bash

#Exit as soon as possible
set -e

if [ $# != 3 ]
then
    echo "Usage test.sh <disk[/dev/sdb]> <unused_raid_device[/dev/md1]> <mount_point>"
    exit 2
fi

RAID_DEV="$2"
BLOCK_DEV="$1"
MNT="$3"

echo "Using drive "$1" as the test drive, the new array would be called "$2" and mounted on "$3
echo "WARNING! All data on the "$1" drive will be destroyed. Are you sure?"
select yn in "Yes" "No"; do
    case $yn in
        Yes ) break;;
        No ) exit;;
    esac
done

echo "Preparing"
umount $MNT 2>/dev/null | /bin/true
mdadm --stop $RAID_DEV 2>/dev/null | /bin/true

echo "Partitioning"
cat test_partitions | sfdisk --force $BLOCK_DEV
hdparm -z $BLOCK_DEV

echo "Creating RAID"
mdadm --create $RAID_DEV --force --run --metadata=1.2 --level=1 --raid-devices=2 $BLOCK_DEV"1" $BLOCK_DEV"2"
mkfs.ext4 $RAID_DEV

echo "Creating test data (it will take some time)"
mount $RAID_DEV $MNT
dd if=/dev/urandom of=$MNT"/testfl" bs=163840 count=65536

sync
sleep 4
echo 3 > /proc/sys/vm/drop_caches
SUM=`md5sum $MNT/testfl`
echo "Checksum is: $SUM"

echo "Preparing to do some TRIM-ing"
../mdtrim.py --raid-device=$RAID_DEV --scratch-dir=$MNT --reserve=113
echo ""
echo "TRIM is finished. Checking if everything's OK"

sync
sleep 4
echo 3 > /proc/sys/vm/drop_caches
SUM2=`md5sum $MNT/testfl`
echo "Checksum is: $SUM2"

if [ "$SUM" != "$SUM2" ]; then
	echo "WARNING!!!! CORRUPTION IS DETECTED!!!!"
	exit 1
fi

echo "Check completed."

echo "Cleaning up"

umount $MNT 2>/dev/null
mdadm --stop $RAID_DEV 2>/dev/null

echo "Done"
