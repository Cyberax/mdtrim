#!/usr/bin/python
# Utility to TRIM unused space on ext4 mdadm RAIDs. Only RAID-1 level is supported,
# though RAID-0 is easy enough to add.
import sys, os, tempfile, time, subprocess, re, uuid, mmap, ctypes
from ctypes import *
from optparse import OptionParser
libc = cdll.LoadLibrary("libc.so.6")

def readln(fl_name):
	fl=open(fl_name, "r")
	try:
		return fl.read().strip()
	finally:
		fl.close()

# Parse options
parser = OptionParser()
parser.add_option("-m", "--raid-device", help="RAID device to cleanup", dest="drive", metavar="/dev/md1")
parser.add_option("-s", "--scratch-dir", metavar="/mnt", 
	help="Directory on the RAID device to use for scratch file", dest="scratch_dir")
parser.add_option("-r", "--reserve", default="4096", metavar="4096",
	help="Space in megabytes to reserve during the trimming", type="int", dest="reserve")
(options, left_args) = parser.parse_args()
if options.drive is None or options.scratch_dir is None:
        parser.error("incorrect number of arguments, use -h for help")

drive = options.drive.replace("/dev/","")
scratch_dir = os.path.abspath(options.scratch_dir)

# Compute defragmenter file size
st = os.statvfs(scratch_dir)
# Compute free space in bytes minus reserved space (given in megabytes) - the result
# is the size of the file that will be used to fill up the space for TRIM-ing.
file_size = (st.f_bavail * st.f_frsize) - options.reserve*(1024**2) 
if file_size <= 1024**2:
	print "Not enough free space on the device %s" % scratch_dir
	sys.exit(1)

print "Scratch directory is %s, trimmer file size is %d GB %d MB" % (scratch_dir, file_size/(1024**3), 
	(file_size % 1024**3) / 1024**2)

path = "/sys/block/"+drive+"/"

lvl_file=path+"md/level"
if not os.path.isfile(lvl_file):
	print "Can't find %s, does it even exist?" % lvl_file
	sys.exit(1)

if readln(lvl_file) <> 'raid1':
	print "Only RAID-1 is supported"
	sys.exit(1)
if readln(path+"md/array_state") not in ['active', 'clean']:
	print "RAID is not in the clean state, exiting"
	sys.exit(1)

slaves = {}

# Determine sector size on the device (it might be 4096 bytes, who knows...?)
sz=os.popen("blockdev --getss /dev/"+drive)
sector_size=int(sz.read().strip())
sz.close()
# And block size
sz=os.popen("blockdev --getbsz /dev/"+drive)
block_size=int(sz.read().strip())
sectors_in_block=block_size/sector_size
sz.close()
file_size = file_size - file_size % block_size

for slave in os.listdir(path+"slaves"):
	# Slave drives are actually symlinks to block devices	
	sl_path=path+"slaves/"+slave
	if not os.path.islink(sl_path):
		raise BaseException("Drive %s is unknown" % sl_path)
	sl_real_path=os.path.realpath(sl_path)

	offset_fl = path+"md/dev-"+slave+"/offset"
	offset_on_array = int(readln(offset_fl))

	# Now calculate offset of the partition on its drive
	offset_partition = int(readln(sl_real_path+"/start"))
	sl_parent_path=os.path.normpath(sl_real_path+"/..")

	# Get device node name
	drive_name = "/dev/"+os.path.basename(sl_parent_path)

	print "Found slave %s on %s with MD offset %d and partition offset %d" % (
		slave, drive_name, offset_on_array, offset_partition)

	dev_fl = os.open(drive_name, os.O_RDONLY | os.O_DIRECT) # Open hard drive directly
	if dev_fl<=0:
		print "Can't open device %s in direct mode" % drive_name
		sys.exit(1)
	slaves[slave]={"offset" : (offset_partition+offset_on_array), "drive" : drive_name,
		"file" : dev_fl}

# Ok, that's it, we have all the information. We can now start the whole TRIM-ming process 
temp_fl = tempfile.NamedTemporaryFile(delete=True, dir=scratch_dir, prefix="filler_for_trim_")

# Analog of hdparm --fallocate file_sz_in_kb temp_file
# This will allocate the blocks and commit them to file, but not actually write data into it.
print "Creating trimmer file"
FALLOC_FL_KEEP_SIZE = c_int(0)
if libc.fallocate(temp_fl.fileno(), c_int(0), 
	c_longlong(0), c_longlong(file_size+block_size*16)) !=0:
	print "Cannot call fallocate on scratch file"
	sys.exit(1)
if libc.fallocate(temp_fl.fileno(), FALLOC_FL_KEEP_SIZE, 
	c_longlong(0), c_longlong(file_size+block_size*16))!=0:
	print "Cannot call fallocate on scratch file"
	sys.exit(1)
temp_fl.flush()
os.fsync(temp_fl.fileno())

# Make sure that our idea of block size matches with HDPARM - I don't have an SSD drive with
# 4096 sector size (they exist, you know)
blockmap=subprocess.Popen(["hdparm", "--fibmap", temp_fl.name], stdout=subprocess.PIPE)
lines = blockmap.stdout.readlines()
if lines[2].find("assuming %d byte sectors" % sector_size) == -1:
	print "We're assuming incorrect sector size %d, "\
		"that's what HDPARM thinks:\n\t'%s'" % (sector_size, lines[2].strip())

print "Writing misalignment detector signatures"
trim_ranges = []
for ln in lines[4:]:
	extent_info=ln.strip().split()
	byte_offset = int(extent_info[0])
	begin_lba = int(extent_info[1])
	end_lba = int(extent_info[2])
	size_in_blocks = int(extent_info[3])

	if (end_lba-begin_lba) > size_in_blocks:
		print "Discrepancy in extent sizes in line: %s" % ln.strip()
		sys.exit(1)

	control = {}

	def create_control(lba):
		if control.has_key(lba):
			return
		# Create sector consisting of UUID, then padded with spaces and ending with another UUID
		data = uuid.uuid4().hex
		data2 = uuid.uuid4().hex
		padding = sector_size - len(data) - len(data2)
		sector_data = data + ' '*padding + data2

		offset = (lba - begin_lba)*sector_size + byte_offset
		temp_fl.seek(long(offset), os.SEEK_SET)
		temp_fl.write(sector_data)
		
		control[lba] = {"sector" : lba, "data" : sector_data, "file_offset" : offset,
				"ln" : (end_lba-begin_lba)*sector_size}

	create_control(begin_lba)
	# Sprinkle some control data in-between
	for cur in range(begin_lba, end_lba, (end_lba-begin_lba)/100+1):
		create_control(cur)
	create_control(end_lba)

	trim_ranges.append({"extent_pos" : begin_lba, "extent_size" : size_in_blocks,  "test_data" : control})

# Make sure we're synced	
temp_fl.flush()
os.fsync(temp_fl.fileno())

# Create aligned string buffer, it must be at least sector_size-aligned
str_buf  = create_string_buffer(block_size*2)
read_buf = ctypes.addressof(str_buf)
read_buf_offset = block_size - read_buf % block_size

print "Checking misalignment signatures"
for sl in slaves.values():
	slave_offset = sl["offset"]*sector_size
	for rng in trim_ranges:
		for test in rng["test_data"].values():
			os.lseek(sl["file"], slave_offset + test["sector"]*sector_size, os.SEEK_SET)			
			if libc.read(sl["file"], read_buf+read_buf_offset, sector_size)!=sector_size:
				print "Can't directly read data from %s" % sl

			what_we_read = string_at(read_buf+read_buf_offset, sector_size)
			if what_we_read != test["data"]:
				print "Data written into the file and data on the physical HD do not match!"
				sys.exit(2)

print "Done signature checking, starting TRIM-ing"
for sl in slaves.values():
	slave_offset = sl["offset"]
	hdparm = subprocess.Popen(["hdparm", "--please-destroy-my-drive", "--trim-sector-ranges-stdin", sl["drive"]], 
		stdin=subprocess.PIPE, stderr=subprocess.STDOUT)

	for rng in trim_ranges:
		begin = rng["extent_pos"]
		len_remain = rng["extent_size"]
		cur_begin = begin
		while len_remain>0:
			cur_len = min(len_remain, 4000)
			# That's it. We're trimming the data here
			hdparm.stdin.write("%d:%d\n" % (cur_begin+slave_offset,cur_len))
			cur_begin = cur_begin + cur_len
			len_remain = len_remain - cur_len
	hdparm.stdin.write("\n")
	hdparm.stdin.close()
	hdparm.wait()
	print "Finished trimming data from slave %s" % sl["drive"]

print "\nTrimming finished. Your data is either safe or hopelessly corrupted."
print "Let's find it out, are you feeling lucky?"
for sl in slaves.values():
	slave_offset = sl["offset"]*sector_size
	for rng in trim_ranges:
		for test in rng["test_data"].values():
			os.lseek(sl["file"], slave_offset + test["sector"]*sector_size, os.SEEK_SET)			
			if libc.read(sl["file"], read_buf+read_buf_offset, sector_size)!=sector_size:
				print "Can't directly read data from %s" % sl

			what_we_read = string_at(read_buf+read_buf_offset, sector_size)
			if what_we_read != '\0'*sector_size and what_we_read != test["data"]:
				print "Well, it appears your data might be damaged. Oops."
				sys.exit(3)

	print "Finished checking data from slave %s" % sl["drive"]
print "Well, it appears that your data is safe"
sys.exit(0)
