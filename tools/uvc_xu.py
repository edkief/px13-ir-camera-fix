import fcntl, struct, ctypes, sys, os
class Q(ctypes.Structure):
    _fields_=[("unit",ctypes.c_uint8),("selector",ctypes.c_uint8),("query",ctypes.c_uint8),("size",ctypes.c_uint16),("data",ctypes.c_void_p)]
def IOWR(t,nr,sz): return (3<<30)|(sz<<16)|(ord(t)<<8)|nr
REQ=IOWR('u',0x21,ctypes.sizeof(Q))
names={0x81:"CUR",0x82:"MIN",0x83:"MAX",0x84:"RES",0x87:"DEF"}
def q(fd,u,s,qq,n,data=None):
    buf=(ctypes.c_uint8*max(n,1))(*(data or []))
    x=Q(u,s,qq,n,ctypes.addressof(buf))
    fcntl.ioctl(fd,REQ,x)
    return bytes(buf)[:n]
def setcur(dev,u,s,hexs):
    fd=os.open(dev,os.O_RDWR); d=bytes.fromhex(hexs); q(fd,u,s,0x01,len(d),list(d)); os.close(fd)
if __name__=="__main__":
  if sys.argv[1]=="set": setcur(sys.argv[2],int(sys.argv[3]),int(sys.argv[4]),sys.argv[5]); sys.exit()
  if True:
    dev=sys.argv[1]; fd=os.open(dev,os.O_RDWR)
    for u,s in [tuple(map(int,a.split(':'))) for a in sys.argv[2:]]:
        try:
            n=struct.unpack('<H',q(fd,u,s,0x85,2))[0]; info=q(fd,u,s,0x86,1)[0]
            print(f"unit {u} sel {s}: len={n} info=0x{info:02x}")
            for k,v in names.items():
                try: print(f"   {v}: {q(fd,u,s,k,n).hex(' ')}")
                except OSError as e: print(f"   {v}: err {e.strerror}")
        except OSError as e: print(f"unit {u} sel {s}: {e}")
    