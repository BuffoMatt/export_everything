#!/usr/bin/env python

# Copyright 2022 Deductiv Inc.
# search_ep_aws_s3.py
# Export Splunk search results to AWS S3 - Search Command
#
# Author: J.R. Murray <jr.murray@deductiv.net>
# Version: 2.0.5 (2022-04-25)

import sys
import os
import random
from deductiv_helpers import setup_logger, replace_keywords, exit_error, replace_object_tokens, recover_parameters, str2bool, log_proxy_settings
from ep_helpers import get_config_from_alias, get_aws_connection
import event_file
from splunk.clilib import cli_common as cli

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
from splunklib.searchcommands import ReportingCommand, dispatch, Configuration, Option, validators

# Define class and type for Splunk command
@Configuration()
class epawss3(ReportingCommand):
	'''
	**Syntax:**
	search | epawss3 target=<target alias> bucket=<bucket> outputfile=<output path/filename> outputformat=[json|raw|kv|csv|tsv|pipe]

	**Description**
	Export Splunk events to AWS S3 (or compatible) over JSON or raw text.
	'''

	#Define Parameters
	target = Option(
		doc='''
		**Syntax:** **target=***<target alias>*
		**Description:** The name of the AWS target alias provided on the configuration dashboard
		**Default:** The target configured as "Default" within the AWS S3 Setup page (if any)''',
		require=False)

	bucket = Option(
		doc='''
		**Syntax:** **bucket=***<bucket name>*
		**Description:** The name of the destination S3 bucket
		**Default:** The bucket name from the AWS S3 Setup page (if any)''',
		require=False)

	outputfile = Option(
		doc='''
		**Syntax:** **outputfile=***<output path/filename>*
		**Description:** The path and filename to be written to the S3 bucket
		**Default:** The name of the user plus the timestamp and the output format, e.g. admin_1588000000.log
			json=.json, csv=.csv, tsv=.tsv, pipe=.log, kv=.log, raw=.log''',
		require=False)

	outputformat = Option(
		doc='''
		**Syntax:** **outputformat=***[json|raw|kv|csv|tsv|pipe]*
		**Description:** The format written for the output events/search results
		**Default:** *csv*''',
		require=False) 

	fields = Option(
		doc='''
		**Syntax:** **fields=***"field1, field2, field3"*
		**Description:** Limit the fields to be written to the S3 file
		**Default:** All (Unspecified)''',
		require=False, validate=validators.List()) 

	compress = Option(
		doc='''
		**Syntax:** **compress=***[true|false]*
		**Description:** Option to compress the output file into .gz format before uploading
		**Default:** The setting from the target configuration, or True if .gz is in the filename ''',
		require=False, validate=validators.Boolean())

	# Validators found @ https://github.com/splunk/splunk-sdk-python/blob/master/splunklib/searchcommands/validators.py

	@Configuration()
	def map(self, events):
		for e in events:
			yield(e)

	#define main function
	def reduce(self, events):
		try:
			app_config = cli.getConfStanza('ep_general','settings')
			cmd_config = cli.getConfStanzas('ep_aws_s3')
		except BaseException as e:
			raise Exception("Could not read configuration: " + repr(e))

		# Facility info - prepended to log lines
		facility = os.path.basename(__file__)
		facility = os.path.splitext(facility)[0]
		try:
			logger = setup_logger(app_config["log_level"], 'export_everything.log', facility)
		except BaseException as e:
			raise Exception("Could not create logger: " + repr(e))

		logger.info('AWS S3 Export search command initiated')
		logger.debug("Configuration: " + str(cmd_config))
		logger.debug('search_ep_awss3 command: %s', self)  # logs command line
		log_proxy_settings(logger)

		# Enumerate settings
		app = self._metadata.searchinfo.app
		user = self._metadata.searchinfo.username
		dispatch = self._metadata.searchinfo.dispatch_dir
		session_key = self._metadata.searchinfo.session_key

		if self.target is None and 'target=' in str(self):
			recover_parameters(self)
		# Replace all tokenized parameter strings
		replace_object_tokens(self)

		# Build the configuration
		try:
			aws_config = get_config_from_alias(session_key, cmd_config, self.target)
			if aws_config is None:
				exit_error(logger, "Unable to find target configuration (%s)." % self.target, 100937)
			#logger.debug("Target configuration: " + str(aws_config)) # This logs the full credential and pass
		except BaseException as e:
			exit_error(logger, "Error reading target server configuration: " + repr(e), 124812)
		
		if self.bucket is None:
			if 'default_s3_bucket' in list(aws_config.keys()):
				t = aws_config['default_s3_bucket']
				if t is not None and len(t) > 0:
					self.bucket = t
				else:
					exit_error(logger, "No bucket specified", 4)
			else:
				exit_error(logger, "No bucket specified", 5)
		
		file_extensions = {
			'raw':  '.log',
			'kv':   '.log',
			'pipe': '.log',
			'csv':  '.csv',
			'tsv':  '.tsv',
			'json': '.json'
		}

		# If the parameters are not supplied or blank (alert actions), supply defaults
		if self.outputformat is None or self.outputformat == "":
			self.outputformat = 'csv'
		if self.fields is not None and self.fields == "":
			self.fields = None # None = All
		if self.outputfile is None or self.outputfile == "":
			# Boto is special. We need repr to give it the encoding it expects to match the hashing.
			self.outputfile = repr('export_' + user + '___now__' + file_extensions[self.outputformat]).strip("'")
		
		# Replace keywords from output filename
		self.outputfile = replace_keywords(self.outputfile)
		self.outputfile = self.outputfile.lstrip('/')

		if self.compress is not None:
			logger.debug('Compression: %s', self.compress)
		else:
			try:
				self.compress = str2bool(aws_config['compress'])
			except:
				self.compress = False
		
		# Use the random number to support running multiple outputs in a single search
		random_number = str(random.randint(10000, 100000))
		staging_filename = 'export_everything_staging_' + random_number + '.txt'
		local_output_file = os.path.join(dispatch, staging_filename)

		# Append .gz to the output file if compress=true
		if not self.compress and len(self.outputfile) > 3:
			# We have a .gz extension when compression was not specified. Enable compression.
			if self.outputfile[-3:] == '.gz':
				self.compress = True
		elif self.compress and len(self.outputfile) > 3:
			if self.outputfile[-3:] != '.gz':
				self.outputfile = self.outputfile + '.gz'

		if self.compress:
			local_output_file = local_output_file + '.gz'
		
		logger.debug("Staging file: %s" % local_output_file)
		try:
			s3 = get_aws_connection(aws_config)
		except BaseException as e:
			exit_error(logger, "Could not connect to AWS: " + repr(e), 741423)
		
		event_counter = 0
		# Write the output file to disk in the dispatch folder
		logger.debug("Writing events to file %s in %s format. Compress=%s\n\tfields=%s", local_output_file, self.outputformat, self.compress, self.fields)
		for event in event_file.write_events_to_file(events, self.fields, local_output_file, self.outputformat, self.compress):
			yield event
			event_counter += 1

		# Upload file to s3
		try:
			with open(local_output_file, "rb") as f:
				s3.upload_fileobj(f, self.bucket, self.outputfile)
			s3 = None
			logger.info("Successfully exported events to s3. app=%s count=%s bucket=%s file=%s user=%s" % (app, event_counter, self.bucket, self.outputfile, user))
			os.remove(local_output_file)
		except s3.exceptions.NoSuchBucket as e:
			exit_error(logger, "Error: No such bucket", 123833)
		except BaseException as e:
			exit_error(logger, "Could not upload file to S3: " + repr(e), 9)

dispatch(epawss3, sys.argv, sys.stdin, sys.stdout, __name__)


