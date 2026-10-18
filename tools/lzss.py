"""LZSS codec bit-exact with the CM110 boot decompressor (0x84060150).
Ring 4096, r0=0xFEE, control byte LSB-first (bit=1 literal, bit=0 match),
match: lo,hi -> pos=((hi&0xF0)<<4)|lo, len=(hi&0xF)+3.
Equivalent to LZ77 w/ 4096 window, len 3..18, pos=(0xFEE+match_start)&0xFFF."""

def decode(src):
    N=4096; ring=bytearray(N); r=0xFEE; out=bytearray()
    i=0; n=len(src); flags=0
    while True:
        flags>>=1
        if (flags & 0x100)==0:
            if i>=n: break
            flags=src[i]|0xFF00; i+=1
        if flags & 1:
            if i>=n: break
            c=src[i]; i+=1; out.append(c); ring[r]=c; r=(r+1)&(N-1)
        else:
            if i+1>=n: break
            lo=src[i]; hi=src[i+1]; i+=2
            pos=((hi&0xF0)<<4)|lo; ln=(hi&0x0F)+3
            for k in range(ln):
                c=ring[(pos+k)&(N-1)]; out.append(c); ring[r]=c; r=(r+1)&(N-1)
    return bytes(out)

def encode(data, max_tries=128):
    N=4096; MINM=3; MAXM=18; n=len(data)
    head={}; prev=[-1]*n
    def h3(i): return data[i] | (data[i+1]<<8) | (data[i+2]<<16)
    tokens=[]; i=0
    def insert(p):
        if p+2<n:
            k=h3(p); prev[p]=head.get(k,-1); head[k]=p
    while i<n:
        best_len=0; best_start=-1
        if i+MINM<=n and i+2<n:
            j=head.get(h3(i),-1); tries=0; maxl=min(MAXM,n-i)
            while j>=0 and (i-j)<N and tries<max_tries:   # d in 1..4095
                if data[j+best_len]==data[i+best_len]:    # quick reject
                    l=0
                    while l<maxl and data[j+l]==data[i+l]: l+=1
                    if l>best_len:
                        best_len=l; best_start=j
                        if l==maxl: break
                j=prev[j]; tries+=1
        if best_len>=MINM:
            tokens.append(('M',best_start,best_len))
            end=i+best_len
            while i<end: insert(i); i+=1
        else:
            tokens.append(('L',data[i])); insert(i); i+=1
    # assemble control bytes + bodies, 8 tokens per control byte, bit0 first
    res=bytearray(); oi=0
    while oi<len(tokens):
        grp=tokens[oi:oi+8]; ctrl=0; body=bytearray()
        for bit,tk in enumerate(grp):
            if tk[0]=='L':
                ctrl |= (1<<bit); body.append(tk[1])
            else:
                _,st,l=tk; pos=(0xFEE+st)&0xFFF
                body.append(pos & 0xFF); body.append(((pos>>4)&0xF0)|((l-3)&0x0F))
        res.append(ctrl); res+=body; oi+=8
    return bytes(res)

def encode_lazy(data, max_tries=4096):
    """LZSS encoder with deflate-style lazy matching (better ratio, slower).
       Bit-exact with decode(). Used when greedy output does not fit a slot."""
    N=4096; MINM=3; MAXM=18; n=len(data)
    head={}; prev=[-1]*n
    def h3(i): return data[i] | (data[i+1]<<8) | (data[i+2]<<16)
    def insert(p):
        if p+2<n:
            k=h3(p); prev[p]=head.get(k,-1); head[k]=p
    def longest(i):
        if i+MINM>n or i+2>=n: return (0,-1)
        best_len=0; best_start=-1; maxl=min(MAXM,n-i)
        j=head.get(h3(i),-1); tries=0
        while j>=0 and (i-j)<N and tries<max_tries:
            if data[j+best_len]==data[i+best_len]:
                l=0
                while l<maxl and data[j+l]==data[i+l]: l+=1
                if l>best_len:
                    best_len=l; best_start=j
                    if l==maxl: break
            j=prev[j]; tries+=1
        return (best_len,best_start)
    tokens=[]; i=0
    while i<n:
        blen,bstart=longest(i)
        if blen>=MINM:
            # lazy: if next position has a strictly longer match, defer (emit literal)
            if blen<MAXM and i+1<n:
                insert(i)
                nlen,_=longest(i+1)
                if nlen>blen:
                    tokens.append(('L',data[i])); i+=1; continue
                # i is already inserted, so emit the match and insert the rest
                tokens.append(('M',bstart,blen))
                end=i+blen; i+=1
                while i<end: insert(i); i+=1
                continue
            tokens.append(('M',bstart,blen))
            end=i+blen
            while i<end: insert(i); i+=1
        else:
            tokens.append(('L',data[i])); insert(i); i+=1
    # assemble (same as encode())
    res=bytearray(); oi=0
    while oi<len(tokens):
        grp=tokens[oi:oi+8]; ctrl=0; body=bytearray()
        for bit,tk in enumerate(grp):
            if tk[0]=='L': ctrl|=(1<<bit); body.append(tk[1])
            else:
                _,st,l=tk; pos=(0xFEE+st)&0xFFF
                body.append(pos&0xFF); body.append(((pos>>4)&0xF0)|((l-3)&0x0F))
        res.append(ctrl); res+=body; oi+=8
    return bytes(res)

if __name__=="__main__":
    import glob,os,struct,time
    # verify round-trip on each decompressed segment + compare sizes to the stock firmware's
    COMB=open("combined_84060000.bin","rb").read(); BASE=0x84060000
    TABLE=[("0x8af90054",0x8407ccaa,0x243),("0x8af90454",0x8407ceed,0xd0),
           ("0x8b01a200",0x840c64ce,0x134d2),("0x8af636f0",0x840688da,0x143d0),
           ("0x8af905cc",0x8407cfbd,0x49511),("0x88010000",0x8406078c,0x814e)]
    for name,src,clen in TABLE:
        comp=COMB[src-BASE:src-BASE+clen]
        dec=decode(comp)
        t=time.time(); re=encode(dec); dt=time.time()-t
        rt=decode(re)
        ok = rt==dec
        print("%s dlen=%#x stock_clen=%#x my_clen=%#x %s roundtrip=%s (%.1fs)"%(
            name,len(dec),clen,len(re),"<=" if len(re)<=clen else ">",ok,dt))
