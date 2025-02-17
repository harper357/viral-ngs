#!/usr/bin/env python
''' This script contains a number of utilities for SNP calling, multi-alignment,
    phylogenetics, etc.
'''

__author__ = "PLACEHOLDER"
__commands__ = []

import Bio.AlignIO
from Bio import SeqIO
import argparse, logging, os, array, bisect

try:
    from itertools import zip_longest
except ImportError :
    from itertools import izip_longest as zip_longest
import tools.muscle, tools.snpeff, tools.mafft
import util.cmd, util.file, util.vcf
from collections import OrderedDict, Sequence

log = logging.getLogger(__name__)

# =========== CoordMapper =================

class CoordMapper(object):
    """ Map (chrom, coordinate) between genome A and genome B.
        Coordinates are 1-based.
        Indels are handled as follows after corresponding sequences are aligned:
            Return (chrom, None) if base is past either end of other sequence.
            If a base maps to a gap in the other species, return the index
                of the closest upstream non-gap base.
            If a base is followed by a gap then instead of returning an integer,
                return a two-element list representing the interval in the
                other species that aligns to this base and the subsequent gap.
        Assumption: the aligner tool will never align two gaps, and will never
            put gaps in opposite species adjacent to each other without aligning
            a pair of real bases in between.
    """
    def __init__(self, fastaA, fastaB, alignerTool = tools.muscle.MuscleTool) :
        """ The two genomes are described by fasta files with the same number of 
            chromosomes, and corresponding chromosomes must be in same order.
        """
        self.AtoB = OrderedDict() # {chrA : [chrB, mapperAB], chrC : [chrD, mapperCD], ...}
        self.BtoA = OrderedDict() # {chrB : [chrA, mapperAB], chrD : [chrC, mapperCD], ...}
        
        self._align(fastaA, fastaB, alignerTool())

    def mapAtoB(self, fromChrom, fromPos = None, side = 0) :
        """ Map (chrom, coordinate) from genome A to genome B.
            If fromPos is None, map only the chromosome name
            If side is:
                < 0, return the left-most position on B
                ==0, return either the unique position on B or a [left,right] list
                > 0, return the right-most position on B
        """
        toChrom, mapper = self.AtoB[fromChrom]
        if fromPos == None:
            return toChrom
        toPos = mapper(fromPos, 0)
        if isinstance(toPos, Sequence) and side != 0 :
            toPos = toPos[0] if side < 0 else toPos[1]
        return (toChrom, toPos)
    
    def mapBtoA(self, fromChrom, fromPos = None, side = 0) :
        """ Map (chrom, coordinate) from genome B to genome A.
            If fromPos is None, map only the chromosome name
            If side is:
                < 0, return the left-most position on A
                ==0, return either the unique position on A or a [left,right] list
                > 0, return the right-most position on A
        """
        toChrom, mapper = self.BtoA[fromChrom]
        if fromPos == None:
            return toChrom
        toPos = mapper(fromPos, 1)
        if isinstance(toPos, Sequence) and side != 0 :
            toPos = toPos[0] if side < 0 else toPos[1]
        return (toChrom, toPos)

    def _align(self, fastaA, fastaB, aligner) :
        # transpose
        per_chr_fastas = transposeChromosomeFiles([fastaA, fastaB])
        if not per_chr_fastas:
            raise Exception('no input sequences')
        # align
        alignOutFileNames = []
        for alignInFileName in per_chr_fastas:
            alignOutFileName = util.file.mkstempfname('.fasta')
            aligner.execute(alignInFileName, alignOutFileName)
            alignOutFileNames.append(alignOutFileName)
            os.unlink(alignInFileName)
        # read in
        self._load_alignments(alignOutFileNames)
        # clean up
        for f in alignOutFileNames:
            os.unlink(f)
    
    def _load_alignments(self, aligned_files, a_idx=0, b_idx=1) :
        assert a_idx>=0 and b_idx>=0
        for alignOutFileName in aligned_files:
            with open(alignOutFileName, 'rt') as alignOutFile :
                seqs = list(SeqIO.parse(alignOutFile, 'fasta'))
                assert a_idx<len(seqs) and b_idx<len(seqs)
                mapper = CoordMapper2Seqs(seqs[a_idx].seq, seqs[b_idx].seq)
                self.AtoB[seqs[a_idx].id] = [seqs[b_idx].id, mapper]
                self.BtoA[seqs[b_idx].id] = [seqs[a_idx].id, mapper]
        assert len(self.AtoB) == len(self.BtoA) == len(aligned_files), \
               'duplicate sequence names'
        

class CoordMapper2Seqs(object) :
    """ Map 1-based coordinates between two aligned sequences.
        Result is a coordinate or an interval, as described in CoordMapper main 
            comment string.
        Return None if beyond end.
        Input sequences must be already-aligned iterators through bases with
            gaps represented by dashes and all other characters assumed to be
            real bases. 
        Assumptions:
            - Sequences (including gaps) are same length.
            - Each sequence has at least one real base.
            - A gap is never aligned to a gap.
            - A gap in one sequence is never adjacent to a gap in the other;
                there must always be an intervening real base between two gaps.
    """
    """
    Implementation:
        mapArrays is a pair of arrays of equal length such that
        (mapArrays[0][n], mapArrays[1][n]) are the coordinates of a pair of
        aligned real bases on the two sequences. The only pairs that are 
        included are the first, the last, and the pair immediately following 
        any gap. Pairs are in increasing order. Coordinate mapping
        requires binary search in one of the arrays.
        Total space required, in bytes, is const + 8 * (number of indels).
        Time for a map in either direction is O(log(number of indels)).
    """
    
    def __init__(self, seq0, seq1) :
        self.mapArrays = [array.array('I'), array.array('I')]
        baseCount0 = 0  # Number of real bases in seq0 up to and including cur pos
        baseCount1 = 0  # Number of real bases in seq1 up to and including cur pos
        beforeStart = True # Haven't yet reached first pair of aligned real bases
        gapSinceLast = False # Have encounted a gap since last pair in mapArrays
        prevRealBase0 = prevRealBase1 = True
        for b0, b1 in zip_longest(seq0, seq1) :
            assert b0 != None and b1 != None, 'CoordMapper2Seqs: sequences '\
                'must be same length.'
            realBase0 = b0 != '-'
            realBase1 = b1 != '-'
            assert realBase0 or realBase1, 'CoordMapper2Seqs: gap aligned to gap.'
            assert (realBase0 or prevRealBase1) and (realBase1 or prevRealBase0),\
                 'CoordMapper2Seqs: gap in one sequence adjacent to gap in other.'
            prevRealBase0 = realBase0
            prevRealBase1 = realBase1
            baseCount0 += realBase0
            baseCount1 += realBase1
            if realBase0 and realBase1 :
                if beforeStart or gapSinceLast :
                    self.mapArrays[0].append(baseCount0)
                    self.mapArrays[1].append(baseCount1)
                    gapSinceLast = False
                    beforeStart = False
                finalPos0 = baseCount0 # Last pair of aligned real bases so far
                finalPos1 = baseCount1 # Last pair of aligned real bases so far
            else :
                gapSinceLast = True
        assert len(self.mapArrays[0]) != 0, 'CoordMapper2Seqs: no aligned bases.'
        if self.mapArrays[0][-1] != finalPos0 :
            self.mapArrays[0].append(finalPos0)
            self.mapArrays[1].append(finalPos1)

    def __call__(self, fromPos, fromWhich) :
        """ fromPos: 1-based coordinate
            fromWhich: if 0, map from 1st sequence to 2nd, o.w. 2nd to 1st."""
        if fromPos != int(fromPos) :
            raise TypeError('CoordMapper2Seqs: pos %s is not an integer' % fromPos)
        fromArray = self.mapArrays[fromWhich]
        toArray = self.mapArrays[1 - fromWhich]
        if fromPos < fromArray[0] or fromPos > fromArray[-1] :
            result = None
        elif fromPos == fromArray[-1] :
            result = toArray[-1]
        else :
            insertInd = bisect.bisect(fromArray, fromPos)
            prevFromPos = fromArray[insertInd - 1]
            nextFromPos = fromArray[insertInd]
            prevToPos = toArray[insertInd - 1]
            nextToPos = toArray[insertInd]
            assert(prevFromPos <= fromPos < nextFromPos)
            prevPlusOffset = prevToPos + (fromPos - prevFromPos)
            if fromPos == nextFromPos - 1 and prevPlusOffset < nextToPos - 1 :
                result = [prevPlusOffset, nextToPos - 1]
            else :
                result = min(prevPlusOffset, nextToPos - 1)
        return result


# ========== snpEff annotation of VCF files ==================

def parser_snpEff(parser=argparse.ArgumentParser()):
    parser.add_argument("inVcf", help="Input VCF file")
    parser.add_argument("genome", help="genome name")
    parser.add_argument("outVcf", help="Output VCF file")
    util.cmd.common_args(parser, (('tmpDir',None), ('loglevel',None), ('version',None)))
    util.cmd.attach_main(parser, tools.snpeff.SnpEff().annotate_vcf, split_args=True)
    return parser
__commands__.append(('snpEff', parser_snpEff))


# =======================
# ***  align_mafft  ***
# =======================

def parser_general_mafft(parser=argparse.ArgumentParser()):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--localpair', default=None, action='store_true',
        help='All pairwise alignments are computed with the Smith-Waterman algorithm.')
    group.add_argument('--globalpair', default=None, action='store_true',
        help='All pairwise alignments are computed with the Needleman-Wunsch algorithm.')
    parser.add_argument('--preservecase', default=None, action='store_true',
        help='Preserve base or aa case, as well as symbols.')
    parser.add_argument('--reorder', default=None, action='store_true',
        help='Output is ordered aligned rather than in the order of the input (default: %(default)s).')
    parser.add_argument('--gapOpeningPenalty', default=1.53, type=float,
        help='Gap opening penalty (default: %(default)s).')
    parser.add_argument('--ep', type=float,
        help='Offset (works like gap extension penalty).')
    parser.add_argument('--verbose', default=False, action='store_true',
        help='Full output (default: %(default)s).')
    parser.add_argument('--outputAsClustal', default=None, action='store_true',
        help='Write output file in Clustal format rather than FASTA')
    parser.add_argument('--maxiters', default = 0, type=int,
        help='Maximum number of refinement iterations (default: %(default)s). Note: if "--localpair" or "--globalpair" is specified this defaults to 1000.')
    parser.add_argument('--threads', default = -1, type=int,
        help='Number of processing threads (default: %(default)s, where -1 indicates use of all available cores).')
    return parser

def parser_align_mafft(parser):
    parser = parser_general_mafft(parser)

    parser.add_argument('inFastas', nargs='+',
        help='Input FASTA files.')
    parser.add_argument('outFile',
        help='Output file containing alignment result (default format: FASTA)')

    util.cmd.common_args(parser, (('loglevel', None), ('version', None), ('tmpDir', None)))
    util.cmd.attach_main(parser, main_align_mafft)
    return parser

def main_align_mafft(args):
    ''' Run the mafft alignment on the input FASTA file.'''

    if int(args.threads) == 0 or int(args.threads) < -1:
        raise argparse.ArgumentTypeError('Argument "--threads" must be non-zero. Specify "-1" to use all available cores.')

    tools.mafft.MafftTool().execute( 
                inFastas          = args.inFastas, 
                outFile           = args.outFile, 
                localpair         = args.localpair, 
                globalpair        = args.globalpair, 
                preservecase      = args.preservecase, 
                reorder           = args.reorder, 
                gapOpeningPenalty = args.gapOpeningPenalty, 
                offset            = args.ep, 
                verbose           = args.verbose, 
                outputAsClustal   = args.outputAsClustal, 
                maxiters          = args.maxiters, 
                threads           = args.threads
    )

    return 0
__commands__.append(('align_mafft', parser_align_mafft))

# =======================
# ***  multichr_mafft  ***
# =======================

def parser_multichr_mafft(parser):
    parser = parser_general_mafft(parser)

    parser.add_argument('inFastas', nargs='+',
        help='Input FASTA files.')
    parser.add_argument('outDirectory', 
        help='Location for the output files (default is cwd: %(default)s)')
    parser.add_argument('--outFilePrefix', default="singlechr",
        help='Prefix for the output file name (default: %(default)s)')

    util.cmd.common_args(parser, (('loglevel', None), ('version', None), ('tmpDir', None)))
    util.cmd.attach_main(parser, multichr_mafft)
    return parser

def multichr_mafft(args):
    ''' Run the mafft alignment on a series of chromosomes provided in sample-partitioned FASTA files. Output as FASTA.
        (i.e. file1.fasta would contain chr1, chr2, chr3; file2.fasta would also contain chr1, chr2, chr3)'''

    if int(args.threads) == 0 or int(args.threads) < -1:
        raise argparse.ArgumentTypeError('Argument "--threads" must be non-zero. Specify "-1" to use all available cores.')

    # get the absolute path to the output directory in case it has been specified as a relative path,
    # since MAFFT relies on its CWD for path resolution
    absoluteOutDirectory = os.path.abspath(args.outDirectory)

    # make the output directory if it does not exist
    if not os.path.isdir( absoluteOutDirectory ):
        os.makedirs( absoluteOutDirectory )

    # prefix for output files
    prefix = "" if args.outFilePrefix == None else args.outFilePrefix

    # reorder the data into new FASTA files, where each FASTA file has only variants of its respective chromosome
    transposedFiles = transposeChromosomeFiles(args.inFastas)

    # since the FASTA files are
    for idx, filePath in enumerate(transposedFiles):
        
        # execute MAFFT alignment. The input file is passed within a list, since argparse ordinarily
        # passes input files in this way, and the MAFFT tool expects lists,
        # but in this case we are creating the input file ourselves
        tools.mafft.MafftTool().execute(
                    inFastas          = [os.path.abspath(filePath)],
                    outFile           = os.path.join(absoluteOutDirectory, "{}_{}.fasta".format(prefix, idx)), 
                    localpair         = args.localpair, 
                    globalpair        = args.globalpair, 
                    preservecase      = args.preservecase, 
                    reorder           = args.reorder, 
                    gapOpeningPenalty = args.gapOpeningPenalty, 
                    offset            = args.ep, 
                    verbose           = args.verbose, 
                    outputAsClustal   = args.outputAsClustal, 
                    maxiters          = args.maxiters, 
                    threads           = args.threads
        )

    return 0
__commands__.append(('multichr_mafft', parser_multichr_mafft))

# ============================

# modified version of rachel's call_snps_3.py follows
def call_snps_3(inFasta, outVcf, REF="KJ660346.2"):
    a=Bio.AlignIO.read(inFasta, "fasta")
    ref_idx = find_ref(a, REF)
    with open(outVcf, 'wt') as outf:
        outf.write(vcf_header(a))
        for row in make_vcf(a, ref_idx, REF):
            outf.write('\t'.join(map(str, row))+'\n')
def find_ref(a, ref):
    for i in range(len(a)):
        if a[i].id == ref:
            return i
    return -1
def vcf_header(a):
    header = "##fileformat=VCFv4.1\n"
    header += "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
    header += "##contig=<ID=\"KM034562\",length=18957>\n"
    header += '#' + '\t'.join(['CHROM','POS','ID','REF','ALT','QUAL','FILTER','INFO','FORMAT'] + [x.id for x in a]) + '\n'
    return header
def make_vcf(a, ref_idx, chrom):
    bases=set(["A", "C", "G", "T"])
    for i in range(len(a[0])):
        alt = []
        for j in range(len(a)):
            if (a[j][i] != a[ref_idx][i]) and ((a[ref_idx][i] in bases) and (a[j][i] in bases)) and a[j][i] not in alt:
                alt.append(a[j][i])
        if len(alt) > 0:
            row = [chrom, i+1, '.', a[ref_idx][i], ','.join(alt), '.', '.', '.', 'GT']
            genos = []
            for k in range(len(a)):
                if a[k][i] == a[ref_idx][i]:
                    genos.append(0)
                elif a[k][i] not in bases:
                    genos.append(".")
                else:
                    for m in range(0, len(alt)):
                        if a[k][i] == alt[m]:
                            genos.append(m+1)
            yield row+genos

def transposeChromosomeFiles(inputFilenamesList):
    ''' Input:  a list of FASTA files representing a genome for each sample.
                Each file contains the same number of sequences (chromosomes, segments,
                etc) in the same order.
        Output: a list of FASTA files representing all samples for each
                chromosome/segment for input to a multiple sequence aligner.
                The number of FASTA files corresponds to the number of chromosomes
                in the genome.  Each file contains the same number of samples
                in the same order.  Each output file is a tempfile.
    '''
    outputFilenames = []

    # open all files
    inputFilesList = [util.file.open_or_gzopen(x, 'rU') for x in inputFilenamesList]
    # get BioPython iterators for each of the FASTA files specified in the input
    fastaFiles = [SeqIO.parse(x, 'fasta') for x in inputFilesList]

    # for each interleaved record
    for chrRecordList in zip_longest(*fastaFiles):
        if any(rec==None for rec in chrRecordList):
            raise Exception("input files must all have the same number of sequences")
        
        outputFilename = util.file.mkstempfname('.fasta')
        outputFilenames.append(outputFilename)
        with open(outputFilename, "w") as outf:
            # write the corresonding records to a new FASTA file
            SeqIO.write(chrRecordList, outf, 'fasta')

    # close all input files
    for x in inputFilesList:
        x.close()

    return outputFilenames

def full_parser():
    return util.cmd.make_parser(__commands__, __doc__)
if __name__ == '__main__':
    util.cmd.main_argparse(__commands__, __doc__)
