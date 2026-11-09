import fcntl, ctypes, os, sys, warnings
warnings.simplefilter("ignore")
class Rect(ctypes.Structure): _fields_=[("l",ctypes.c_int32),("t",ctypes.c_int32),("w",ctypes.c_uint32),("h",ctypes.c_uint32)]
class Ext(ctypes.Structure): _pack_=1; _fields_=[("id",ctypes.c_uint32),("size",ctypes.c_uint32),("r2",ctypes.c_uint32),("val",ctypes.c_uint64)]
class Ctrls(ctypes.Structure): _fields_=[("which",ctypes.c_uint32),("count",ctypes.c_uint32),("err",ctypes.c_uint32),("fd",ctypes.c_int32),("r",ctypes.c_uint32),("ctrls",ctypes.c_void_p)]
S=(3<<30)|(ctypes.sizeof(Ctrls)<<16)|(ord('V')<<8)|72
fd=os.open('/dev/video2',os.O_RDWR)
l,t,w,h,auto=map(int,sys.argv[1:6])
r=Rect(l,t,w,h); e=(Ext*2)(Ext(0x00981ae1,ctypes.sizeof(r),0,ctypes.addressof(r)),Ext(0x00981ae2,0,0,auto))
c=Ctrls(0x00980000,2,0,0,0,ctypes.addressof(e)); fcntl.ioctl(fd,S,c); print("ok")
