import os
import sys
import getopt

import vtrace
import vtrace.tools.win32heap as win32heap
import vtrace.tools.win32aslr as win32_aslr
import vtrace.tools.win32stealth as win32_stealth
import vtrace.util as v_util

import envi.memory as e_mem
import envi.cli as e_cli
import envi.bits as e_bits

import PE
import vstruct.defs.pe as vs_pe

def teb(vdb, line):
    """
    Print out the TEB for the current or specified thread.

    Usage: teb [threadid]
    """
    t = vdb.getTrace()
    threads = t.getThreads()
    tid = t.getMeta("ThreadId")
    if len(line):
        tid = t.parseExpression(line)
    taddr = threads.get(tid)
    if taddr == None:
        vdb.vprint("Invalid Thread ID: %d" % tid)
        return
    teb = t.getStruct("ntdll.TEB", taddr)
    vdb.vprint(teb.tree(va=taddr, reprmax=32))

def peb(vdb, line):
    """
    Print the PEB

    Usage: peb
    """
    t = vdb.getTrace()
    t.requireAttached()
    pebaddr = t.getMeta("PEB")
    peb = t.getStruct("ntdll.PEB", pebaddr)
    vdb.vprint(peb.tree(pebaddr, reprmax=32))

def regkeys(vdb, line):
    """
    Show all the registry keys the target process currently has open.

    Usage: regkeys
    """
    t = vdb.getTrace()
    t.requireAttached()
    vdb.vprint("\nOpen Registry Keys:\n")
    for fd,ftype,fname in t.getFds():
        if ftype == vtrace.FD_REGKEY:
            vdb.vprint("\t%s" % fname)
    vdb.vprint("")

def einfo(vdb, line):
    """
    Show all the current exception information.

    -P    Toggle the "PendingSignal" meta key which controls
          delivery (or handling) of the current exception.

    Usage: einfo [options]
    """
    argv = e_cli.splitargs(line)
    t = vdb.getTrace()

    try:
        opts,args = getopt.getopt(argv, 'P')
    except Exception, e:
        return vdb.do_help('einfo')

    for opt,optarg in opts:
        if opt == '-P':
            p = t.getMeta('PendingSignal')
            if p != None:
                t.setMeta('OrigSignal', p)
                t.setMeta('PendingSignal', None)
            else:
                newp = t.getMeta('OrigSignal', None)
                t.setMeta('PendingSignal', newp)

    exc = t.getMeta("Win32Event", None)
    if exc == None:
        vdb.vprint("No Exception Information Found")
    ecode = exc.get("ExceptionCode", 0)
    eaddr = exc.get("ExceptionAddress",0)
    chance = 2
    if exc.get("FirstChance", False):
        chance = 1

    einfo = exc.get("ExceptionInformation", [])
    #FIXME get extended infoz
    #FIXME unify with cli thing
    vdb.vprint("Win32 Exception 0x%.8x at 0x%.8x (%d chance)" % (ecode, eaddr, chance))
    vdb.vprint("Exception Information: %s" % " ".join([hex(i) for i in einfo]))
    dbool = True
    if t.getCurrentSignal() == None:
        dbool = False
    vdb.vprint('Deliver Exception: %s' % dbool)

def seh(vdb, line):
    """
    Walk and print the SEH chain for the current (or specified) thread.

    Usage: seh [threadid]
    """
    t = vdb.getTrace()
    if len(line) == 0:
        tid = t.getMeta("ThreadId")
    else:
        tid = int(line)
    tinfo = t.getThreads().get(tid, None)
    if tinfo == None:
        vdb.vprint("Unknown Thread Id: %d" % tid)
        return
    teb = t.getStruct("ntdll.TEB", tinfo)
    addr = long(teb.NtTib.ExceptionList)
    vdb.vprint("REG        HANDLER")
    while addr != 0xffffffff:
        #FIXME print out which frame these are in
        er = t.getStruct("ntdll.EXCEPTION_REGISTRATION_RECORD", addr)
        vdb.vprint("0x%.8x 0x%.8x" % (addr, er.Handler))
        addr = long(er.Next)

def safeseh(vdb, line):
    """
    Show the SafeSEH status of all the loaded DLLs or list the
    handlers for a particular dll by normalized name.

    Usage: safeseh [libname]
    """
    t = vdb.getTrace()
    libs = t.getMeta("LibraryBases")
    if len(line):
        base = libs.get(line)
        if base == None:
            vdb.vprint("Unknown library: %s" % line)
            return

        vdb.vprint("%s:" % line)

        try:
            p = PE.peFromMemoryObject(t, base)
        except Exception, e:
            vdb.vprint('Error: %s (0x%.8x) %s' % (line, base, e))
            return

        if p.IMAGE_LOAD_CONFIG != None:
            va = int(p.IMAGE_LOAD_CONFIG.SEHandlerTable)
            if va != 0:
                count = int(p.IMAGE_LOAD_CONFIG.SEHandlerCount)
                for h in t.readMemoryFormat(va, "<%dL" % count):
                    vdb.vprint("\t0x%.8x %s" % (base+h, vdb.reprPointer(base+h)))
                return
        vdb.vprint("None...")
            
    else:
        lnames = libs.keys()
        lnames.sort()
        for name in lnames:
            base = libs.get(name)
            try:
                p = PE.peFromMemoryObject(t, base)
            except Exception, e:
                vdb.vprint('Error: %s (0x%.8x) %s' % (name, base, e))
                continue

            enabled = False
            if p.IMAGE_LOAD_CONFIG != None:
                va = int(p.IMAGE_LOAD_CONFIG.SEHandlerTable)
                if va != 0:
                    enabled = True
                #print name
                #print p.IMAGE_LOAD_CONFIG
            vdb.vprint("%16s\t%s" % (name, enabled))

def validate_heaps(db):
    """
    A simple routine that works like the built in windows
    heap checkers to show where blocks and/or freelist
    is potentially dorked.
    """
    trace = db.getTrace()
    db.vprint("Validating:")
    for heap in win32heap.getHeaps(trace):
        db.vprint("%s: 0x%.8x" % ("heap".rjust(9), heap.address))

        try:
            f = heap.getFreeLists()
        except Exception, e:
            #import traceback
            #traceback.print_exc()
            db.vprint("%s: %s" % (e.__class__.__name__,e))

        for seg in heap.getSegments():
            db.vprint("%s: 0x%.8x" % ("segment".rjust(9),seg.address))
            try:
                blist = seg.getChunks()
            except Exception, e:
                db.vprint("%s: %s" % (e.__class__.__name__,e))

def heaps(vdb, line):
    """
    Show Win32 Heap Information.

    Usage: heaps [-F <heapaddr>] [-C <address>] [-L <segmentaddr>]
    -F <heapaddr> print the freelist for the heap
    -C <address>  Find and print the heap chunk containing <address>
    -S <segmentaddr> Print the chunks for the given heap segment
    -L <heapaddr> Print the look aside list for the given heap
    -V Validate the heaps (check next/prev sizes and free list)
    -l <heapaddr> Leak detection (list probable leaked chunks)
    -U <heapaddr> Show un-commited ranges for the specified heap
    (no options lists heaps and segments)
    """
    t = vdb.getTrace()
    t.requireAttached()

    argv = e_cli.splitargs(line)
    freelist_heap = None
    chunkfind_addr = None
    chunklist_seg = None
    lookaside_heap = None
    leakfind_heap = None
    uncommit_heap = None
    try:
        opts,args = getopt.getopt(argv, "F:C:S:L:l:U:V")
    except Exception, e:
        return vdb.do_help('heaps')

    for opt,optarg in opts:
        if opt == "-F":
            freelist_heap = t.parseExpression(optarg)
        elif opt == "-C":
            chunkfind_addr = t.parseExpression(optarg)
        elif opt == "-L":
            lookaside_heap = t.parseExpression(optarg)
        elif opt == "-S":
            chunklist_seg = t.parseExpression(optarg)
        elif opt == "-V":
            return validate_heaps(vdb)
        elif opt == "-l":
            leakfind_heap = t.parseExpression(optarg)
        elif opt == '-U':
            uncommit_heap = t.parseExpression(optarg)

    if lookaside_heap != None:
        haddrs = [h.address for h in win32heap.getHeaps(t)]
        if lookaside_heap not in haddrs:
            vdb.vprint("0x%.8x is NOT a valid heap!" % lookaside_heap)
            return

        heap = win32heap.Win32Heap(t, lookaside_heap)
        vdb.vprint('[Index] [Chunks]')
        for i,l in enumerate(heap.getLookAsideLists()):
            vdb.vprint("[%d]" % i)
            for c in l:
                vdb.vprint("    %s" % (repr(c)))

    elif uncommit_heap != None:

        haddrs = [h.address for h in win32heap.getHeaps(t)]
        if uncommit_heap not in haddrs:
            vdb.vprint("0x%.8x is NOT a valid heap!" % uncommit_heap)
            return

        heap = win32heap.Win32Heap(t, uncommit_heap)
        ucrdict = heap.getUCRDict()
        addrs = ucrdict.keys()
        addrs.sort()
        if len(addrs) == 0:
            vdb.vprint('Heap 0x%.8x has 0 uncommited-ranges!' % uncommit_heap)
            return

        vdb.vprint('Uncommited ranges for heap: 0x%.8x' % uncommit_heap)
        for ucraddr in addrs:
            size = ucrdict.get(ucraddr)
            vdb.vprint('0x%.8x (%d)' % (ucraddr, size))

        return

    elif freelist_heap != None:
        haddrs = [h.address for h in win32heap.getHeaps(t)]
        if freelist_heap not in haddrs:
            vdb.vprint("0x%.8x is NOT a valid heap!" % freelist_heap)
            return

        heap = win32heap.Win32Heap(t, freelist_heap)
        for i,l in enumerate(heap.getFreeLists()):
            if len(l):
                vdb.vprint("Freelist Index: %d" % i)
                for c in l:
                    vdb.vprint("   %s" % repr(c))

    elif chunkfind_addr != None:
        heap,seg,chunk = win32heap.getHeapSegChunk(t, chunkfind_addr)
        vdb.vprint("Address  0x%.8x found in:" % (chunkfind_addr,))
        vdb.vprint("Heap:    0x%.8x" % (heap.address))
        vdb.vprint("Segment: 0x%.8x" % (seg.address))
        vdb.vprint("Chunk:   0x%.8x (%d) FLAGS: %s" % (chunk.address, len(chunk),chunk.reprFlags()))

    elif chunklist_seg != None:

        for heap in win32heap.getHeaps(t):
            for seg in heap.getSegments():
                if chunklist_seg == seg.address:
                    vdb.vprint("Chunks for segment at 0x%.8x (X == in use)" % chunklist_seg)
                    for chunk in seg.getChunks():
                        c = " "
                        if chunk.isBusy():
                            c = "X"
                        vdb.vprint("0x%.8x %s (%d)" % (chunk.address,c,len(chunk)))
                    return

        vdb.vprint("Segment 0x%.8x not found!" % chunklist_seg)

    elif leakfind_heap != None:
        # FIXME do this the slow way for now...
        haddrs = [h.address for h in win32heap.getHeaps(t)]
        if leakfind_heap not in haddrs:
            vdb.vprint("0x%.8x is NOT a valid heap!" % leakfind_heap)
            return

        h = win32heap.Win32Heap(t, leakfind_heap)
        for seg in h.getSegments():
            for chunk in seg.getChunks():
                if chunk.address == seg.address:
                    continue
                # Obviously, only check for leaks if they are in use...
                # FIXME we will need to check the lookaside also...
                if not chunk.isBusy():
                    continue
                addr = chunk.getDataAddress()
                # FIXME get size and endian from trace
                pat = e_bits.buildbytes(addr, 4)
                l = t.searchMemory(pat)
                if len(l) == 0:
                    vdb.vprint("0x%.8x may be leaked!" % addr)

    else:
        vdb.vprint("Heap\t\tSegment")
        for heap in win32heap.getHeaps(t):
            flags = " ".join(heap.getFlagNames())
            for s in heap.getSegments():
                vdb.vprint("0x%.8x\t0x%.8x\t%s" % (heap.address, s.address, flags))

IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE = 0x0040

def showaslr(vdb, base, libname):
    t = vdb.getTrace()
    try:
        p = PE.peFromMemoryObject(t, base)
    except Exception, e:
        vdb.vprint('Error: %s (0x%.8x) %s' % (name, base, e))
        return
    enabled = False
    c = p.IMAGE_NT_HEADERS.OptionalHeader.DllCharacteristics
    if c & IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE:
        enabled = True
    vdb.vprint("%16s\t%s" % (libname, enabled))

def aslr(vdb, line):
    """
    Determine which PE's in the current process address space
    support Vista's ASLR implementation by the presence of the
    IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE (0x0040) bit in the 
    DllCharacteristics field of the PE header.

    Usage: aslr [libname]
    """
    t = vdb.getTrace()
    libs = t.getMeta("LibraryBases")
    if line:
        base = libs.get(line)
        if base == None:
            vdb.vprint("Unknown library: %s" % line)
            return
        showaslr(vdb, base, line)
    else:
        lnames = libs.keys()
        lnames.sort()
        for name in lnames:
            base = libs.get(name)
            showaslr(vdb, base, name)

def _printPageHits(vdb, hits, unique=False):
    vdb.vprint('[  eip  ]  [ mem addr ] [ access ]')
    if unique:
        newhits = []
        [newhits.append(h) for h in hits if not newhits.count(h)]
        hits = newhits

    for eip,addr,perm in hits:
        vdb.vprint("0x%.8x 0x%.8x   %s" % (eip,addr,e_mem.getPermName(perm)))

def pagewatch(vdb, line):
    """
    Enable write access watching on a given memory page.  This works
    by setting the page to read-only and then specially handling the
    access violations as though they were hardware Watchpoints.

    Usage: pagewatch [options] [<addr_expression>]
    -C - Clear the current pagewatch log
    -F - Toggle auto-continue behavior (run and record vs. stop on hit)
         (NOTE: use this with 'mode FastBreak=True' for *really* fast!
    -L - List the current hits from the pagewatch log
    -M - Add page watches to the entire memory map from addr_expression
    -R - Use to enable *read* watching while adding a page watch
    -S <addr> - Show touches to the specified address
    -P <addr> - Show memory touched by specifed program counter (eip)
    -u - When listing, show only *unique* entries
    """
    argv = e_cli.splitargs(line)
    try:
        opts,args = getopt.getopt(argv, "CFLMP:RS:u")
    except Exception, e:
        return vdb.do_help('pagewatch')

    if vdb.trace.getMeta('pagewatch') == None:
        vdb.trace.setMeta('pagewatch', [])

    if vdb.trace.getMeta('pagerun') == None:
        vdb.trace.setMeta('pagerun', False)

    domap = False
    unique = False
    watchread = False
    for opt,optarg in opts:

        if opt == "-C":
            vdb.trace.setMeta("pagewatch", [])
            vdb.vprint("Pagewatch log cleared")
            return

        elif opt == '-F':
            pr = vdb.trace.getMeta('pagerun', False)
            pr = not pr
            vdb.trace.setMeta('pagerun', pr)
            vdb.vprint('Pagewatch Auto Continue: %s' % pr)
            return

        elif opt == "-L":
            hits = vdb.trace.getMeta('pagewatch', [])
            _printPageHits(vdb, hits, unique=unique)
            return

        elif opt == "-M":
            domap = True

        elif opt == '-R':
            watchread = True

        elif opt == "-S":
            saddr = vdb.trace.parseExpression(optarg)
            hits = vdb.trace.getMeta("pagewatch")
            if hits == None:
                vdb.vprint("No pagewatch log!")
                return
            hits = [ h for h in hits if h[1] == saddr ]
            _printPageHits(vdb, hits, unique=unique)
            return

        elif opt == "-P":
            saddr = vdb.trace.parseExpression(optarg)
            hits = vdb.trace.getMeta("pagewatch")
            if hits == None:
                vdb.vprint("No pagewatch log!")
                return

            hits = [ h for h in hits if h[0] == saddr ]
            _printPageHits(vdb, hits, unique=unique)
            return

        elif opt == '-u':
            unique = True

    if len(args) == 0:
        return vdb.do_help('pagewatch')

    baseaddr = vdb.trace.parseExpression(args[0])
    # Page align
    baseaddr = baseaddr & 0xfffff000
    maxaddr = baseaddr + 4096

    map = vdb.trace.getMemoryMap(baseaddr)
    if map == None:
        raise Exception("Invalid memory map address 0x%.8x" % baseaddr)

    if domap:
        baseaddr = map[0]
        maxaddr  = baseaddr + map[1]

    bpset = vdb.trace.breakpoints
    while baseaddr < maxaddr:
        # Skip ones that are already there!
        if not bpset.get(baseaddr):
            wp = vtrace.PageWatchpoint(baseaddr, size=4096, watchread=watchread)
            wpid = vdb.trace.addBreakpoint(wp)
        baseaddr += 4096

def stealth(vdb, line):
    """
    Enable basic debugger stealth.  This has the following effects:

    Change PEB to show BeingDebugged == 0
    Special breakpoint on CheckRemoteDebuggerPresent

    WARNING:
    break/sendBreak() behave VERY strange with this because the
    kernel aparently doesn't think he needs to post the exception
    to the debugger?
    """
    if vdb.trace.getMeta("Win32Stealth") != None:
        win32_stealth.unstealthify(vdb.trace)
        vdb.vprint("Stealth disabled")
    else:
        win32_stealth.stealthify(vdb.trace)
        vdb.vprint("Stealth enabled")


gflag_stuff = [
    ('loader_snaps', 'ntdll.ShowSnaps', '<B', 0, 1),
    ('loader_debug', 'ntdll.LdrpDebugFlags', '<I', 0, 0xffffffff),
]

def gflags(vdb, line):
    '''
    Support a subset of gflags like behavior on windows.  This enables
    features *exclusively* by direct process manipulation and does NOT
    set any registry settings or persist across processes...

    Usage: gflags [toggle_type]

    NOTE: Most of these options require symbols!
    '''
    argv = e_cli.splitargs(line)

    optnames = [ x[0] for x in gflag_stuff ]

    for opt in argv:

        if opt not in optnames:
            vdb.vprint('Unknown/Unsupported Option: %s' % opt)
            continue

        for hname, symname, fmt, offval, onval in gflag_stuff:
            if opt == hname:
                try:
                    addr = vdb.trace.parseExpression(symname)
                    cur = vdb.trace.readMemoryFormat(addr, fmt)[0]
                    if cur == offval:
                        newval = onval
                    else:
                        newval = offval
                    vdb.trace.writeMemoryFormat(addr, fmt, newval)
                except Exception, e:
                    vdb.vprint('Symbol Failure: %s' % symname)
                break

    for hname, symname, fmt, offval, onval in gflag_stuff:
        status = 'Unknown'
        try:
            addr = vdb.trace.parseExpression(symname)
            val = vdb.trace.readMemoryFormat(addr, fmt)[0]
            if val == offval:
                status = 'Off'
            elif val == onval:
                status = 'On'
        except Exception, e:
            pass
        vdb.vprint('%s : %s' % (hname.rjust(20), status))


def pe(vdb, line):
    """
    Show extended info about loaded PE binaries.

    Usage: pe [opts] [<libname>...]
    -I      Show PE import files.
    -m      Toggle inmem/ondisk behavior (directly mapped DLLs)
    -N      Show full NT header
    -t      Show PE timestamp information
    -E      Show PE exports
    -S      Show PE sections
    -v      Show FileVersion from VS_VERSIONINFO
    -V      Show all keys from VS_VERSIONINFO
    """
    #-v      Show PE version information
    argv = e_cli.splitargs(line)
    try:
        opts,args = getopt.getopt(argv, "EImNStvV")
    except Exception, e:
        return vdb.do_help('pe')

    inmem = True

    showsecs = False
    showvers = False
    showtime = False
    showimps = False
    shownthd = False
    showexps = False
    showvsin = False
    for opt,optarg in opts:
        if opt == '-I':
            showimps = True
        elif opt == '-t':
            showtime = True
        elif opt == '-v':
            showvers = True
        elif opt == '-V':
            showvsin = True
        elif opt == '-N':
            shownthd = True
        elif opt == '-m':
            inmem = False
        elif opt == '-S':
            showsecs = True
        elif opt == '-E':
            showexps = True

    t = vdb.trace
    bases = t.getMeta("LibraryBases")
    paths = t.getMeta("LibraryPaths")

    names = args
    if len(names) == 0:
        names = t.getNormalizedLibNames()

    names.sort()
    names = e_cli.columnstr(names)
    for libname in names:
        base = bases.get(libname.strip(), None)
        if base == None:
            base = vdb.trace.parseExpression(libname)
        path = paths.get(base, "unknown")

        try:
            pobj = PE.peFromMemoryObject(t, base)
        except Exception, e:
            vdb.vprint('Error: %s (0x%.8x) %s' % (libname, base, e))
            continue

        if showimps:
            ldeps = {}
            try:
                for rva,lname,fname in pobj.getImports():
                    ldeps[lname.lower()] = True
                lnames = ldeps.keys()
                lnames.sort()
                vdb.vprint('0x%.8x - %.30s %s' % (base, libname, ' '.join(lnames)))
            except Exception, e:
                vdb.vprint('Import Parser Error On %s: %s' % (libname, e))

        elif showvers:
            version = 'Unknown!'
            vs = pobj.getVS_VERSIONINFO()
            if vs != None:
                version = vs.getVersionValue('FileVersion')
            vdb.vprint('%s: %s' % (libname.rjust(30),version))

        elif showvsin:
            vs = pobj.getVS_VERSIONINFO()
            vdb.vprint('==== %s' % libname)
            if vs == None:
                vdb.vprint('no VS_VERSIONINFO...')
            else:
                vskeys = vs.getVersionKeys()
                vskeys.sort()
                for vskey in vskeys:
                    vsval = vs.getVersionValue(vskey)
                    vdb.vprint('%s: %s' % (vskey.rjust(20), vsval[:50]))

        elif showtime:
            tstamp = pobj.IMAGE_NT_HEADERS.FileHeader.TimeDateStamp
            vdb.vprint('0x%.8x - %.30s 0x%.8x' % (base, libname, tstamp))

        elif shownthd:
            t = pobj.IMAGE_NT_HEADERS.tree(reprmax=32)
            vdb.vprint(t)

        elif showsecs:
            for sec in pobj.getSections():
                vdb.vprint(sec.tree(reprmax=32))

        elif showexps:
            vdb.vprint('[Ord] [Address] [Name]')
            for fva, ord, name in pobj.getExports():
                vdb.vprint('%.4d 0x%.8x %s' % (ord, fva, name))
        else:
            vdb.vprint('0x%.8x - %.30s %s' % (base, libname, path))

def bindiff(mem1, mem2):
    ret = []
    i = 0
    imax = len(mem1)
    while i < imax:
        r = i
        while mem1[r] != mem2[r] and r < imax:
            r += 1
        # We found a discrepency
        if r != i:
            size = (r-i)
            ret.append((i,size))
            i+=r
        i+=1
    return ret

def deaslr(vdb, line):
    '''
    Rebase the specified address expression as though the origin
    library had gotten it's suggested base address rather than
    being ASLR'd.

    Usage: deaslr <addr_expr>
    '''
    if len(line) == 0:
        return vdb.do_help('deaslr')

    addr = vdb.trace.parseExpression(line)
    newaddr = win32_aslr.deAslr(vdb.trace, addr)

    vdb.vprint('aslr: 0x%.8x deaslr: 0x%.8x' % (addr, newaddr))

def sympath(vdb, line):
    '''
    Set the symbol path for the tracer.  This will currently only
    effect *subsequent* library loads!

    Usage: sympath <new_path>
    '''
    if len(line):
        vdb.trace.setMeta('NtSymbolPath', line)
    sympath = vdb.trace.getMeta('NtSymbolPath')
    if sympath == None:
        sympath = os.getenv('_NT_SYMBOL_PATH')
    vdb.vprint('Current Symbol Path: %s' % sympath)

def stepb(vdb, line):
    '''
    Use the extended intel hardware support to step to the next branch
    target.

    Usage: stepb
    
    NOTE: This will *not* work inside VMware / VirtualBox.  Other hypervisors
          may vary... (it will simply single step)
    '''
    if len(line):
        vdb.do_help('stepb')

    orig = vdb.trace.getMode('BranchStep')
    vdb.trace.setMode('BranchStep', True)
    # For now, lets cheat so we get FastStep behavior for free...
    vdb.do_stepi('')
    vdb.trace.setMode('BranchStep', orig)

def hooks(vdb, line):
    '''
    Check the executable regions of the target process for any
    hooks by comparing against the PE on disk.  This will
    account for relocations and import entries.
    '''
    t = vdb.getTrace()
    bases = t.getMeta("LibraryBases")
    paths = t.getMeta("LibraryPaths")
    found = False
    for bname in bases.keys():
        base = bases.get(bname)
        fpath = paths.get(base)
        pobj = PE.PE(file(fpath,'rb'))
        filebase = pobj.IMAGE_NT_HEADERS.OptionalHeader.ImageBase

        skips = {}
        # Get relocations for skipping
        r = (0,1,2,3)
        for relrva, reltype in pobj.getRelocations():
            for i in r:
                skips[base+relrva+i] = True
        # Add the import entries to skip
        for iva,libname,name in pobj.getImports():
            for i in r:
                skips[base+iva+i] = True

        for sec in pobj.getSections():
            if sec.Characteristics & PE.IMAGE_SCN_MEM_EXECUTE:
                size = sec.VirtualSize
                va = base + sec.VirtualAddress
                fileva = filebase + sec.VirtualAddress
                filebytes = pobj.readAtRva(sec.VirtualAddress, sec.VirtualSize)
                procbytes = t.readMemory(va, size)
                for off,size in bindiff(filebytes, procbytes):
                    difva = va + off
                    fdifva = fileva + off
                    # Check for a relocation covering this...
                    if skips.get(difva):
                        continue
                    found = True
                    dmem = procbytes[off:off+size].encode('hex')[:10]
                    dfil = filebytes[off:off+size].encode('hex')[:10]

                    vdb.canvas.addVaText('0x%.8x' % difva, difva)
                    vdb.canvas.addText(' (0x%.8x) (%d)' % (fdifva,size))
                    vdb.canvas.addText(' mem: %s file: %s ' % (dmem, dfil))

                    sym = vdb.symobj.getSymByAddr(difva, exact=False)
                    if sym != None:
                        vdb.canvas.addText(' ')
                        vdb.canvas.addVaText('%s + %d' % (repr(sym),difva-long(sym)), difva)
                    vdb.canvas.addText('\n')

    if not found: vdb.canvas.addText('No Hooks Found!\n')

def jit(vdb, line):
    '''
    Enable/Disable the current VDB location as the current Just-In-Time
    debugger for windows applications.

    Usage: jitenable [-D]
    -E  Enable VDB JIT debugging
    -D  Disable JIT debugging
    '''
    argv = e_cli.splitargs(line)
    try:
        opts,args = getopt.getopt(argv, "ED")
    except Exception, e:
        return vdb.do_help('jit')

    try:
        import _winreg
    except Exception, e:
        vdb.vprint('Error Importing _winreg: %s' % e)
        return

    HKLM = _winreg.HKEY_LOCAL_MACHINE
    HKCU = _winreg.HKEY_CURRENT_USER
    REG_SZ = _winreg.REG_SZ

    regpath = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\AeDebug'
    #wow64path = r'SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\AeDebug'

    #regkey = _winreg.CreateKey(HKLM, regpath)
    regkey = _winreg.CreateKey(HKLM, regpath)

    vdb.vprint('JIT Currently: %s' % _winreg.QueryValueEx(regkey, 'Debugger')[0])

    setval = None
    for opt,optarg in opts:

        if opt == '-D':
            setval = ''

        elif opt == '-E':
            vdbpath = os.path.abspath(sys.argv[0])
            setval = '%s %s -r -p %%ld -e %%Id' % (sys.executable, vdbpath)
            #_winreg.SetValue(HKLM

    if setval != None:
        vdb.vprint('Setting JIT: %s' % (setval,))
        _winreg.SetValueEx(regkey, 'Debugger', None, REG_SZ, setval)

def svclist(vdb, line):
    '''
    List the running service names and pids.

    Usage: svclist
    '''
    cols = []
    pids = []
    names = []
    descrs = []
    for pid, name, descr in vdb.trace._getSvcList():
        pids.append('%d' %  pid)
        names.append(name)
        descrs.append(descr)

    names = e_cli.columnstr(names)

    for i in xrange(len(pids)):
        vdb.vprint('%8s %s %s' % (pids[i], names[i], descrs[i]))

def injectso(vdb, line):
    '''
    Inject a shared object (DLL) into the target process.

    Usage: injectso <dllname>
    '''
    if not line:
        return vdb.do_help('injectso')
    t = vdb.trace
    t.injectso(line)

token_elevation_types = {
    0: 'UAC Not Present',
    1: 'Default Elevation',
    2: 'Elevated',
    3: 'Low',
}
def uac(db, line):
    '''
    Display the current UAC status of the target process.
    (User Account Control)

    Usage: uac
    '''
    t = db.trace
    u = t._getUacStatus()
    db.vprint('UAC Status: %s' % token_elevation_types.get(u))

# The necissary module extension function
def vdbExtension(db, trace):
    db.registerCmdExtension(pe)
    db.registerCmdExtension(peb)
    db.registerCmdExtension(einfo)
    db.registerCmdExtension(heaps)
    db.registerCmdExtension(regkeys)
    db.registerCmdExtension(seh)
    db.registerCmdExtension(safeseh)
    db.registerCmdExtension(teb)
    db.registerCmdExtension(pagewatch)
    db.registerCmdExtension(stealth)
    db.registerCmdExtension(aslr)
    db.registerCmdExtension(hooks)
    db.registerCmdExtension(gflags)
    #db.registerCmdExtension(deaslr)
    db.registerCmdExtension(sympath)
    db.registerCmdExtension(jit)
    db.registerCmdExtension(svclist)
    #db.registerCmdExtension(stepb)
    db.registerCmdExtension(injectso)
    db.registerCmdExtension(uac)
