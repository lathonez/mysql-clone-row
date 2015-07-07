#! /usr/local/bin/python
""" Python module for cloning a MYSQL row from one host to another """

import MySQLdb as mdb
import ConfigParser, time, datetime, argparse, os, stat
from DictDiffer import DictDiffer

class CloneRow(object):
    """ CloneRow constructor, doesn't take any args """

    def __init__(self):
        self.config = ConfigParser.ConfigParser(allow_no_value=True)
        self.config.readfp(open('CloneRow.cfg'))
        self.source_host = None
        self.target_host = None
        self.source_con = None
        self.target_con = None
        self.database = {
            'name': None,
            'table': None,
            'column': None,
            'column_filter': None,
            'ignore_columns': []
        }
        self.target_insert = False

    def parse_cla(self):
        """ parse command line arguments """
        parser = argparse.ArgumentParser()
        parser.add_argument('source_host', help='source hostname: should be defined in config')
        parser.add_argument('target_host', help='target hostname: should be defined in config')
        parser.add_argument('database', help='database name: same on source and target host')
        parser.add_argument('table', help='table to consider: select from <table>')
        parser.add_argument(
            'column',
            help='column to consider: select from table where <column>'
        )
        parser.add_argument(
            'column_filter',
            help='value to filter column: select from table where column = <column_filter>'
        )
        args = parser.parse_args()
        self.source_host = args.source_host
        self.target_host = args.target_host
        self.database['name'] = args.database
        self.database['table'] = args.table
        self.database['column'] = args.column
        self.database['column_filter'] = args.column_filter
        self.get_table_config(self.database['table'])

    def get_table_config(self, table):
        """ get table specific config items, if any """
        table_section = 'table.' + table
        if not self.config.has_section(table_section):
            print 'get_table_config: no table specific config defined'
            return
        try:
            self.database['ignore_columns'] = self.config.get(
                table_section, 'ignore_columns'
            ).rsplit(',')
            ignore_string = 'The following columns will be ignored: '
            for column in self.database['ignore_columns']:
                ignore_string += column + ' '
            print ignore_string
        except ConfigParser.NoOptionError:
            print 'get_table_config: no ignore_columns for ' + self.database['table']
            return

    def error(self, message):
        """ wrapper for raising errors that does housekeeping too """
        self.housekeep()
        raise Exception('FATAL: ' + message)

    def connect(self, user, host, port, password=None):
        """ connect to a mysql database, returning a MySQLdb.Connection object """
        if password is not None:
            con = mdb.connect(
                host=host,
                user=user,
                db=self.database['name'],
                port=port,
                passwd=password
            )
        else:
            con = mdb.connect(
                host=host,
                user=user,
                db=self.database['name'],
                port=port
            )
        version = con.get_server_info()
        print 'Connected to {0}@${1}:{2} - Database version : {3} '.format(
            user, host, self.database['name'], version
        )
        return con

    def get_row(self, con):
        """ Run a select query (MYSQLdb.Connection.query)
            returning a dict including column headers.
            Should always return a single row.
        """
        # we're not using cursors here because we want the nice object with column headers
        select_sql = 'select * from {0} where {1} = {2}'.format(
            self.database['table'],
            self.database['column'],
            self.quote_sql_param(con.escape_string(self.database['column_filter']))
        )
        con.query(select_sql)
        res = con.store_result()
        # we should only _ever_ be playing with one row, per host, at a time
        if res.num_rows() == 0:
            return None
        if res.num_rows() != 1:
            self.error('get_row: Only one row expected -- cannot clone on multiple rows!')

        row = res.fetch_row(how=1)
        return dict(row[0])

    @classmethod
    def check_config_chmod(cls):
        """ make sure the read permissions of CloneRow.cfg are set correctly """
        chmod = oct(stat.S_IMODE(os.stat('CloneRow.cfg').st_mode))
        print chmod
        if chmod != '0600':
            print 'CloneRow.cfg needs to be secure\n\nchmod 0600 CloneRow.cfg\n\n'
            raise Exception('FATAL: CloneRow.cfg is insecure')

    @classmethod
    def find_deltas(cls, source_row, target_row):
        """ use DictDiffer to find what's different between target and source """
        delta = DictDiffer(source_row, target_row)
        return {
            'new_columns_in_source': delta.added(),
            'new_columns_in_target': delta.removed(),
            'delta_columns': delta.changed(),
            'unchanged_columns': delta.unchanged()
        }

    @classmethod
    def quote_sql_param(cls, sql_param):
        """ 'quote' a param if necessary, else return it as is.
            param should be escaped (Connection.escape_string) before it's passed in
            we should use cursors and parameterisation where possible, but sometimes we
            need to use the Connection.query method, so this is necessary
        """
        if isinstance(sql_param, str) or isinstance(sql_param, datetime.datetime):
            return '\'{0}\''.format(sql_param)
        else:
            # doesn't need quoting
            return sql_param

    def get_column_sql(self, con, column):
        """ return sql to add or drop a given column from the table we're working on """
        drop_sql = 'alter table `{0}` drop column `{1}`;'.format(self.database['table'], column)
        con.query('show fields from {0} where field = \'{1}\''.format(
            self.database['table'],
            column
        ))
        res = con.store_result()
        if res.num_rows() != 1:
            self.error('get_column_sql: only one row expected!')
        column_info = dict(res.fetch_row(how=1)[0])
        not_null = '' if column_info['Null'] == 'yes' else ' not null'
        default = '' if column_info['Default'] is None else ' default ' + column_info['Default']
        add_sql = 'alter table `{0}` add column `{1}` {2}{3}{4};'.format(
            self.database['table'],
            column,
            column_info['Type'],
            default,
            not_null
        )
        return {
            'add_sql': add_sql,
            'drop_sql': drop_sql
        }

    def show_ddl_updates(self, mode, deltas):
        """ display SQL statements to adjust database for column deltas
            mode: (source|target)
            con: database connection (for source or target)
            table: table we're working on
            deltas: column differences
        """
        working_db = self.source_host if mode == 'source' else self.target_host
        other_db = self.target_host if mode == 'source' else self.source_host
        con = self.source_con if working_db == self.source_host else self.target_con

        for column in deltas:
            print '\n|----------------------|column: {0}|----------------------|\n'.format(column)
            print '\'{0}\' exists in the {1} database but not in the source {2}\n'.format(
                column, working_db, other_db
            )
            info = self.get_column_sql(con, column)
            print 'ADD: to add column \'{0}\' to {1}, run the following SQL on {2}:\n'.format(
                column, other_db, other_db)
            print info['add_sql'], '\n'
            print 'DROP: to drop column \'{0}\' from {1}, run the following SQL on {2}:\n'.format(
                column, working_db, working_db)
            print info['drop_sql'], '\n'
            print '|-----------------------{0}-----------------------|'.format(
                '-' * len('column: ' + column)
            )

    def update_target(self, source_row, deltas):
        """ update the data in the target database with differences from source """
        # TODO:
        #   - dump raw update sql
        #   - do it in one statement
        if not len(deltas):
            return
        cur = self.target_con.cursor()
        # generate update sql for everything in the deltas
        for column in deltas:
            if column in self.database['ignore_columns']:
                continue
            # doing updates one by one is just easier and more readable
            update_sql = "update {0} set {1} = %s where {2} = %s".format(
                self.database['table'],
                column,
                self.database['column']
            )
            # run the update
            print 'updating {0}.{1}'.format(self.database['table'], column)
            cur.execute(update_sql, (source_row[column], self.database['column_filter'],))
            if self.target_con.affected_rows() != 1:
                self.target_con.rollback()
                cur.close()
                self.error('update_target: expected to update a single row')
        # don't commit anything until all updates have gone in ok
        cur.close()
        self.target_con.commit()
        return

    def unload_target(self):
        """ unload the row we're working on from the target_db in case we ruin it """
        print 'backing up target row..'
        cur = self.target_con.cursor()
        unload_file = self.config.get('backup', 'unload_dir')
        unload_file += '/{0}-{1}-{2}-{3}'.format(
            self.database['table'],
            self.database['column'],
            self.database['column_filter'],
            int(round(time.time() * 1000))
        )
        cur.execute('select * into outfile \'{0}\' from {1} where {2} = %s'.format(
            unload_file,
            self.database['table'],
            self.database['column']
        ), (self.database['column_filter'], ))
        if self.target_con.affected_rows() != 1:
            self.error('unload_target: unable to verify unload file')
        print 'backup file can be found at {0} on {1}'.format(unload_file, self.target_host)
        return unload_file

    def restore_target(self, unload_file):
        """ restore data unloaded from unload_target """
        cur = self.target_con.cursor()
        delete_sql = 'delete from {0} where {1} = %s'.format(
            self.database['table'],
            self.database['column']
        )
        cur.execute(delete_sql, (self.database['column_filter'], ))
        if self.target_con.affected_rows() != 1:
            cur.close()
            self.target_con.rollback()
            self.error('restore_target: expected to delete only one row')
        if self.target_insert:
            print 'not restoring from backup as target was inserted from scratch'
            cur.close()
            self.target_con.commit()
            return
        restore_sql = 'load data infile \'{0}\' into table {1}'.format(
            unload_file,
            self.database['table']
        )
        cur.execute(restore_sql)
        if self.target_con.affected_rows() != 1:
            cur.close()
            self.target_con.rollback()
            self.error('restore_target: expected to load only one row')
        cur.close()
        self.target_con.commit()

    def print_delta_columns(self, deltas):
        """ helper function to print out columns which will be updated """
        print '\n\n|----------------------------------------------------------|'
        print 'The following columns will be updated on ' + self.target_host
        for column in deltas:
            if column in self.database['ignore_columns']:
                continue
            print '\t- ' + column
        print '|----------------------------------------------------------|\n'

    @classmethod
    def user_happy(cls):
        """ Give the user a chance to restore from backup easily beforer we terminate """
        print 'Row has been cloned successfully..'
        print 'Type \'r\' to (r)estore from backup, anything else to termiate'
        descision = raw_input()
        if descision == 'r':
            print 'restoring from backup..'
            return False
        else:
            return True

    def minimal_insert(self):
        """ insert as little data as possible into the target database
            this will allow us to reselect and continue as normal if
            the row doesn't exist at all
        """
        # TODO - we could find all the columns that require default values
        #        and spam defaults of the appropriate datatype in there..
        cur = self.target_con.cursor()
        insert_sql = 'insert into {0} ({1}) values (%s)'.format(
            self.database['table'],
            self.database['column']
        )
        cur.execute(insert_sql, (self.database['column_filter'],))
        if self.target_con.affected_rows() != 1:
            cur.close()
            self.error('somehow we\'ve inserted multiple rows')
        # now we have a row, we can return it as usual
        return self.get_row(self.target_con)

    def print_restore_sql(self, backup):
        """ tell the user how to rollback by hand after script has run """
        restore_manual_sql = '  begin;\n'
        restore_manual_sql += '  delete from {0} where {1} = {2};\n'.format(
            self.database['table'],
            self.database['column'],
            self.quote_sql_param(self.database['column_filter'])
        )
        restore_manual_sql += '  -- the above should have delete one row, '
        restore_manual_sql += 'if not, run: rollback;\n'
        restore_manual_sql += '  load data infile \'{0}\' into table {1};\n'.format(
            backup, self.database['table']
        )
        restore_manual_sql += '  commit;\n'
        print '\n|------------------------------------------------------------|\n'
        print ' To rollback manually, run the following sql on {0}\n'.format(
            self.target_host)
        print restore_manual_sql
        print '|------------------------------------------------------------|'
        return

    def housekeep(self):
        """ close connections / whatever else """
        print 'housekeeping..'
        self.source_con.close()
        self.target_con.close()

    def main(self):
        """ main method """
        self.check_config_chmod()
        self.parse_cla()
        print 'connecting to source database..'
        self.source_con = self.connect(
            self.config.get(self.source_host, 'username'),
            self.config.get(self.source_host, 'host'),
            self.config.getint(self.source_host, 'port'),
            self.config.get(self.source_host, 'password')
        )
        print 'connecting to target database..'
        self.target_con = self.connect(
            self.config.get(self.target_host, 'username'),
            self.config.get(self.target_host, 'host'),
            self.config.getint(self.target_host, 'port'),
            self.config.get(self.target_host, 'password')
        )
        # we don't want mysql commit stuff unless we've okay'd it
        self.target_con.autocommit(False)
        print 'getting source row..'
        source_row = self.get_row(self.source_con)
        if source_row is None:
            self.error('row does not exist in source database')
        print 'getting target row..'
        target_row = self.get_row(self.target_con)
        if target_row is None:
            print 'row does not exist at all in target, running a minimal insert..'
            self.target_insert = True
            target_row = self.minimal_insert()
        print 'finding deltas..'
        deltas = self.find_deltas(source_row, target_row)
        self.show_ddl_updates('source', deltas['new_columns_in_source'])
        self.show_ddl_updates('target', deltas['new_columns_in_target'])
        if not len(deltas['delta_columns']):
            print '\ndata is identical in target and source, nothing to do..'
            self.housekeep()
            return True

        if set(deltas['delta_columns']).issubset(set(self.database['ignore_columns'])):
            print '\nall deltas ignored - [table.{0}]:ignore_columns'.format(
                self.database['table']
            )
            self.housekeep()
            return True
        self.print_delta_columns(deltas['delta_columns'])
        backup = self.unload_target()
        self.update_target(source_row, deltas['delta_columns'])
        if not self.user_happy():
            self.restore_target(backup)
        else:
            print 'operation completed successfully, have a fantastic day'
            self.print_restore_sql(backup)
        self.housekeep()

DOLLY = CloneRow()
DOLLY.main()
