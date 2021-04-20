import os
import sys
import numpy as np

import datajoint as dj

dj.config['database.prefix'] = os.environ.get('DJ_PREFIX', '')
schema = dj.schema(dj.config['database.prefix'] + 'pipeline_lab')

@schema
class Paths(dj.Lookup):
    definition = """
        global       : varchar(255)               # global path name
        ---
        linux        : varchar(255)               # linux path name
        windows      : varchar(255)               # windows path name
        mac          : varchar(255)               # mac path name
        location     : varchar(255)               # computer path
    """

    def get_local_path(self, path, local_os=None):

        # TODO keep or remove?
        # determine local os
        # if local_os is None:
        #     local_os = sys.platform
        #     local_os = local_os[:(min(3, len(local_os)))]
        # if local_os.lower() == 'glo':
        #     local = 0
        #     home = '~'

        # elif local_os.lower() == 'lin':
        #     local = 1
        #     home = os.environ['HOME']

        # elif local_os.lower() == 'win':
        #     local = 2
        #     home = os.environ['HOME']

        # elif local_os.lower() == 'dar':
        #     local = 3
        #     home = '~'

        # else:
        #     raise NameError('unknown OS')

        # path = path.replace(os.path.sep, '/')
        # path = path.replace('~', home)

        # mapping = np.asarray(self.fetch('global', 'linux', 'windows', 'mac'))
        # size = mapping.shape
        # for i in range(size[1]):
        #     for j in range(size[0]):
        #         n = len(mapping[j, i])
        #         if j != local and path[:n] == mapping[j, i][:n]:
        #             path = os.path.join(mapping[local, i], path[n+1:])
        #             break

        # if os.path.sep == '\\' and local_os.lower() != 'glo':
        #     path = path.replace('/', '\\')

        # else:
        #     path = path.replace('\\', '/')

        # TODO implement with environment variables
        if path.split(':')[0] == 'L':
            root_dir = '/data/external_scratch08'        
        elif path.split(':')[0] == 'N':
            root_dir = '/data/external_scratch10'
        path = os.path.join(root_dir, path.split(':')[1])
        
        path = path.replace('\\', '/')
        print(path)

        return path