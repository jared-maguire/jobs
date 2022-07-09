from setuptools import setup

setup(
    name='k8s',
    version='0.1.0',    
    description='Run python functions on Kubernetes',
    url='https://github.com/jared-maguire/jobs',
    author='Jared Maguire',
    author_email='jared.maguire@gmail.com',
    license='MIT',
    packages=['k8s'],
    install_requires=['dill',
                      'requests',
                      'jinja2',
                      ],

    classifiers=[
        #'Development Status :: 1 - Planning',
        #'Intended Audience :: Science/Research',
        #'License :: OSI Approved :: BSD License',  
        #'Operating System :: POSIX :: Linux',        
        #'Programming Language :: Python :: 2',
        #'Programming Language :: Python :: 2.7',
        #'Programming Language :: Python :: 3',
        #'Programming Language :: Python :: 3.4',
        #'Programming Language :: Python :: 3.5',
    ],
)