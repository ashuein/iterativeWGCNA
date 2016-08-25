# pylint: disable=invalid-name
# pylint: disable=unused-import
# pylint: disable=bare-except
# pylint: disable=broad-except
# pylint: disable=too-many-instance-attributes
'''
main application
'''

from __future__ import print_function

import logging
import sys
import os
from time import strftime

import rpy2.robjects as ro
from .genes import Genes
from .expression import Expression
from .eigengenes import Eigengenes
from .wgcna import WgcnaManager
from .io.utils import create_dir, read_data, warning, transpose_file_contents
from .r.utils import initialize_r_workspace
from .r.imports import base, wgcna, rsnippets


class IterativeWGCNA(object):
    '''
    main application
    '''

    def __init__(self, args):
        self.args = args
        create_dir(self.args.workingDir)
        
        self.__initialize_log()
        self.logger.info(strftime("%c"))

        self.profiles = None
        self.genes = None
        self.eigengenes = Eigengenes()
      
        self.passCount = 1
        self.iterationCount = 1
        self.iteration = None # unique label for iteration

        self.algorithmConverged = False
        self.passConverged = False

        self.__initialize_R()
        self.__log_parameters()

        # load expression data and
        # initialize Genes object
        # to store results
        self.__load_expression_profiles()
        self.__log_input_data()

        self.genes = Genes(self.profiles)


    def run_pass(self, passGenes):
        '''
        run a single pass of iterative WGCNA
        (prune data until no more residuals are found)
        '''

        iterationGenes = passGenes
        while not self.passConverged:
            self.run_iteration(iterationGenes)

            moduleCount = self.genes.count_modules(iterationGenes)
            classifiedGeneCount = self.genes.count_classified_genes(iterationGenes)
            
            self.write_gene_counts(len(iterationGenes), classifiedGeneCount)

            # if there are no residuals
            # (classified gene count = number of genes input)
            # then the pass has converged
            if classifiedGeneCount == len(iterationGenes):
                self.passConverged = True
            else:
                # run again with genes classified in current pass
                iterationGenes = self.genes.get_classified_genes(iterationGenes)
                self.iterationCount = self.iterationCount + 1

            # if no modules were detected,
            # then the algorithm has converged
            # exit the pass
            if moduleCount == 0:
                self.algorithmConverged = True
                self.passConverged = True
                self.__log_alogorithm_converged()


    def run_iterative_wgcna(self):
        '''
        run iterative WGCNA
        '''
        if self.args.verbose:
            warning("Beginning iterations")

        # genes involved in current iteration
        passGenes = self.profiles.genes()

        while not self.algorithmConverged:
            self.run_pass(passGenes)
            classifiedGeneCount = self.genes.count_classified_genes(passGenes)
            self.__log_pass_completion()
            self.__log_gene_counts(len(passGenes), classifiedGeneCount)

            if not self.algorithmConverged:
                # set residuals as new gene list
                passGenes = self.genes.get_unclassified_genes()

                # increment pass counter and reset iteration counter
                self.passCount = self.passCount + 1
                self.iterationCount = 1

                # reset pass convergence flag
                self.passConverged = False

        self.iteration = 'final'
        self.genes.iteration = self.iteration
        self.merge_close_modules()
        self.reassign_genes_to_best_fit_module()

        self.genes.write(True)
        self.eigengenes.write(True)
        self.transpose_output_files()



    def run(self):
        '''
        main function --> makes calls to run iterativeWGCNA,
        catches errors, and logs time
        '''

        try:
            self.run_iterative_wgcna()
            self.logger.info('iterativeWGCNA: SUCCESS')
        except Exception:
            if self.logger is not None:
                self.logger.exception('iterativeWGCNA: FAIL')
            else:
                raise
        finally:
            if self.logger is not None:
                self.logger.info(strftime("%c"))


    def transpose_output_files(self):
        '''
        transpose output files to make them human
        readable
        '''
        if self.args.verbose:
            warning("Generating final output")

        # transpose membership and kME files (so samples are columns)
        transpose_file_contents('pre-pruning-membership.txt', 'Gene')
        transpose_file_contents('membership.txt', 'Gene')
        transpose_file_contents('eigengene-connectivity.txt', 'Gene')


    def reassign_genes_to_best_fit_module(self):
        '''
        use kME goodness of fit to reassign module
        membership
        '''
        if self.args.verbose:
            warning("Making final goodness of fit assessment")

        count = self.genes.reassign_to_best_fit(self.eigengenes,
                                                self.args.wgcnaParameters['reassignThreshold'],
                                                self.args.wgcnaParameters['minKMEtoStay'])
        self.logger.info("Reassinged " + str(count) + " genes in final kME review.")
        if self.args.verbose:
            warning("Reassinged " + str(count) + " genes in final kME review.")


    def merge_close_modules(self):
        '''
        merge close modules based on similiarity in eigengenes
        update membership, kME, and eigengenes accordingly
        '''
        if self.args.verbose:
            warning("Extracting final eigengenes and merging close modules")

        modules = self.genes.get_modules()
        self.__log_final_modules(modules)

        self.eigengenes.load_matrix_from_file('eigengenes.txt')
        self.eigengenes.update_to_subset(modules)

        self.eigengenes = self.genes.merge_close_modules(self.eigengenes,
                                                         self.args.moduleMergeCutHeight)
        # self.logger.debug("FINAL EIGENGENES")
        # self.logger.debug(self.eigengenes.matrix)


    def run_iteration(self, iterationGenes):
        '''
        run an iteration of blockwise WGCNA
        '''
        self.__generate_iteration_label()

        if self.args.verbose:
            warning("Iteration: " + self.iteration)

        self.genes.iteration = self.iteration
        iterationProfiles = self.profiles.gene_expression(iterationGenes)

        blocks = self.run_blockwise_wgcna(iterationProfiles)
        if self.args.saveBlocks:
            rsnippets.saveObject(blocks, 'blocks', 'blocks-' + self.iteration + '.RData')

        # update eigengenes from blockwise result
        # if eigengenes are present (modules detected), evaluate
        # fitness and update gene module membership
        self.eigengenes.extract_from_blocks(self.iteration, blocks, self.profiles.samples())

        if not self.eigengenes.is_empty():
            self.eigengenes.write(False)

            # extract membership from blocks and calc eigengene connectivity
            self.genes.update_membership(iterationGenes, blocks)
            self.genes.update_kME(self.eigengenes)
            self.genes.write(False) # output before pruning

            self.genes.evaluate_fit(self.args.wgcnaParameters['minKMEtoStay'],
                                    iterationGenes)
            self.genes.remove_small_modules(self.args.wgcnaParameters['minModuleSize'])
            self.genes.write(True) # output after pruning


    def run_blockwise_wgcna(self, exprData):
        '''
        run WGCNA
        '''
        manager = WgcnaManager(exprData, self.args.wgcnaParameters)
        manager.set_parameter('saveTOMFileBase', self.iteration + '-TOM')
        return manager.blockwise_modules()


    def __generate_iteration_label(self):
        '''
        generates the unique label for the iteration
        '''
        self.iteration = 'p' + str(self.passCount) + '_iter_' + str(self.iterationCount)


    def __load_expression_profiles(self):
        # gives a weird R error that I'm having trouble catching
        # when it fails
        # TODO: identify the exact exception
        try:
            self.profiles = Expression(read_data(self.args.inputFile))
        except:
            self.logger.error("Unable to open input file: " + self.args.inputFile)
            sys.exit(1)


    def __initialize_R(self):
        '''
        initialize R workspace and logs
        '''
        initialize_r_workspace(self.args.workingDir, self.args.enableWGCNAThreads)


    def __initialize_log(self):
        '''
        initialize log by setting path and file format
        '''
        logging.basicConfig(filename=self.args.workingDir + '/iterativeWGCNA.log',
                            filemode='w', format='%(levelname)s: %(message)s',
                            level=logging.DEBUG)

        logging.captureWarnings(True)
        self.logger = logging.getLogger(__name__)


    def __log_alogorithm_converged(self):
        '''
        log algorithm convergence
        '''
        message = "No modules detected for iteration " + self.iteration \
                  + ". Classification complete."
        self.logger.info(message)
        if self.args.verbose:
            warning(message)


    def __log_parameters(self):
        '''
        log WGCNA parameter choices and working
        directory name
        '''
      
        self.logger.info("Working directory: " + self.args.workingDir)
        self.logger.info("Saving blocks for each iteration? "
                         + ("TRUE" if self.args.saveBlocks else "FALSE"))
        self.logger.info("Merging final modules if cutHeight <= "
                         + str(self.args.moduleMergeCutHeight))
        self.logger.info("Allowing WGCNA Threads? "
                         + ("TRUE" if self.args.enableWGCNAThreads else "FALSE"))
        self.logger.info("Running WGCNA with the following params:")
        self.logger.info(self.args.wgcnaParameters)

        if self.args.verbose:
            warning("Working directory: " + self.args.workingDir)
            warning("Allowing WGCNA Threads? "
                    + ("TRUE" if self.args.enableWGCNAThreads else "FALSE"))
            warning("Running WGCNA with the following params:")
            warning(self.args.wgcnaParameters)


    def __log_input_data(self):
        '''
        log input details
        '''
        self.logger.info("Loaded file: " + self.args.inputFile)
        self.logger.info(str(self.profiles.ncol()) + " Samples")
        self.logger.info(str(self.profiles.nrow()) + " Genes")
        if self.args.verbose:
            warning("Loaded file: " + self.args.inputFile)
            warning(str(self.profiles.ncol()) + " Samples")
            warning(str(self.profiles.nrow()) + " Genes")


    def __log_pass_completion(self):
        '''
        summarize pass in log when convergence condition is met
        '''
        message = "Pass " + str(self.passCount) + " converged on iteration " \
                  + str(self.iterationCount) + "."
        self.logger.info(message)
        if self.args.verbose:
            warning(message)


    def __log_gene_counts(self, initialGeneCount, classifiedGeneCount):
        '''
        log classified gene count
        '''
        message = " FIT: " + str(classifiedGeneCount) + "; RESIDUAL: "
        message = message + str(initialGeneCount - classifiedGeneCount)
        self.logger.info(message)
        if self.args.verbose:
            warning(message)


    def __log_final_modules(self, modules):
        '''
        log modules
        '''
        message = "Found " + str(len(modules)) + " modules."
        self.logger.info(message)
        self.logger.info(modules)
        if self.args.verbose:
            warning(message)
            warning(modules)



    def write_gene_counts(self, initial, fit):
        '''
        writes the number of kept and dropped genes at the end of an iteration
        '''
        fileName = 'gene-counts.txt'
        try:
            os.stat(fileName)
        except OSError:
            header = ('Iteration', 'Initial', 'Fit', 'Residual')
            with open(fileName, 'a') as f:
                print('\t'.join(header), file=f)
        finally:
            with open(fileName, 'a') as f:
                print('\t'.join((self.iteration, str(initial),
                                 str(fit), str(initial - fit))), file=f)

