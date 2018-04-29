import argparse
import sys
import os
from datetime import datetime

from logzero import logger
from apscheduler.schedulers.blocking import BlockingScheduler


from .core.config import setup, access, DatabaseConfig
from .core.helpers import read_csv, csv_with_content, get_timewindow
from .core.verbalautopsy import Event, verbal_autopsy_factory
from .core.exceptions.base import SmartVADHIS2Exception
from .core.exceptions.errors import ImportException, DuplicateEventImportError


def _parse_args(args=sys.argv[1:]):
    """Parse arguments"""
    description = u"Download briefcases, run SmartVA and import to DHIS2.\n" \
                  u"If no arguments are provided it is scheduled and run in a sliding time window mode."
    parser = argparse.ArgumentParser(usage='%(prog)s', description=description)

    group = parser.add_mutually_exclusive_group()

    group.add_argument(u'--manual',
                       dest='manual',
                       action='store',
                       required=False,
                       help=u"Skip download of briefcase file, provide local file path instead"
                       )

    group.add_argument(u'--all',
                       dest='all',
                       action='store_true',
                       default=False,
                       required=False,
                       help=u"Pull all briefcases instead of relative time window"
                       )

    arguments = parser.parse_args(args)
    if arguments.manual and not os.path.exists(arguments.manual):
        raise FileNotFoundError(u"Briefcase file does not exist: {}".format(arguments.manual))
    return arguments


def _schedule():
    """
    Background scheduler that runs forever, schedules to (other) SQLite database
    """
    scheduler = BlockingScheduler()
    url = r'sqlite:///{}'.format(os.path.join(DatabaseConfig.database_dir, 'scheduling.db'))
    scheduler.add_jobstore('sqlalchemy', url=url)
    SECONDS = 30
    scheduler.add_job(_run,
                      'interval',
                      seconds=SECONDS,
                      args=[False, False],
                      replace_existing=True,
                      id='smartva-dhis2-runner',
                      next_run_time=datetime.now())
    logger.info("Started scheduling every {} seconds".format(SECONDS))
    scheduler.start()


def _run(manual, download_all):
    """
    Method with the application logic.
    """
    dhis, briefcase, smartva, db = access()

    if manual:
        smartva_file = smartva.run(manual, manual=True)
    else:
        briefcase_file = briefcase.download_briefcases(download_all)
        smartva_file = None
        print(briefcase_file)
        if csv_with_content(briefcase_file):
            smartva_file = smartva.run(briefcase_file)
        else:
            logger.warn("No new ODK records to process for time window {} - {}".format(*get_timewindow()))

    success_count, error_count, no_of_records = 0, 0, 0
    if csv_with_content(smartva_file):
        no_of_records = sum(1 for _ in read_csv(smartva_file))
        for i, record in enumerate(read_csv(smartva_file), 1):
            logger.info("{0} [{1}/{2}] SID: {3} {0}".format('---------', i, no_of_records, record.get('sid')))
            va, exc, warnings = verbal_autopsy_factory(record)
            logger.info(record)

            if warnings:
                [logger.warn(w) for w in warnings]

            if exc:
                [logger.error(e) for e in exc]
                db.write_errors(record, exc)
                error_count += 1
            else:
                event = Event(va)
                try:
                    dhis.is_duplicate(va.sid)
                except DuplicateEventImportError as e:
                    logger.exception("Record for ID {} already exists in DHIS2".format(record.get('sid')))
                    db.write_errors(record, e)
                    error_count += 1
                else:
                    try:
                        dhis.post_event(event.payload)
                    except ImportException as e:
                        logger.exception("{}\nfor payload {}".format(e, event.payload))
                        db.write_errors(record, e)
                        error_count += 1
                    else:
                        logger.info("Import successful!")
                        success_count += 1

    logger.info("SUMMARY: Parsed ODK records: {} | Successful imports: {} | Errors: {}".format(no_of_records, success_count, error_count))


def launch():
    try:
        opts = _parse_args()
        setup()
        if not any([opts.manual, opts.all]):
            try:
                _schedule()
            except (KeyboardInterrupt, SystemExit):
                pass
        else:
            _run(manual=opts.manual, download_all=opts.all)
    except KeyboardInterrupt:
        logger.warning("Aborted!")
    except Exception as e:
        logger.exception(e)
