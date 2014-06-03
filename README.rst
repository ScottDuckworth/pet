=======================
Puppet Environment Tool
=======================

The Puppet Environment Tool (pet) fills in some gaps in connecting your Puppet_
servers to your `dynamic environments`_.

.. _Puppet: http://puppetlabs.com/
.. _`dynamic environments`: http://puppetlabs.com/blog/git-workflow-and-puppet-environments

Configuration
=============

pet recognizes the following configuration files:

* ``~/.pet.conf``
* ``/etc/pet.conf``

You can also specify another file with the ``--config`` option.

Configuration files use ini syntax.  Each section specifies an alternate
configuration, and the default section is just called ``default``.

The following configuration options are available:

``remote``
  The URL of the git repository containing your Puppet code.  Any valid git URL
  should work.

``backend`` or ``backend.name``
  The command used to contact a Puppet server.  See below for details.

``cachedir``
  A cache directory.  For best results, put this on the same file system as
  ``environmentpath``.  Defaults to ``/var/cache/pet``.

``environmentpath``
  The path where your Puppet environments reside.  Defaults to
  ``/etc/puppet/environments``.

``puppet``
  The name of the puppet command.  Defaults to ``puppet``.

``librarian_puppet``
  The name of the librarian-puppet command.  Defaults to ``librarian-puppet``.

``git``
  The name of the git command.  Defaults to ``git``.

Deploying Puppet Code from Hosted Git Repositories
==================================================

Assume the following setup:

* Your master git repository is hosted on Bitbucket_ or GitHub_.
* You have a CGI-capable web server accessible by Bitbucket or GitHub.
* Your web server can reach your Puppet servers via SSH.

.. _Bitbucket: http://bitbucket.org
.. _GitHub: http://github.com

Create a CGI script on your web server which executes the ``pet cgi``
subcommand::

  #!/bin/sh
  exec /path/to/pet cgi

Configure your repository on Bitbucket or GitHub for a POST hook, pointing it
to the URL of the above CGI script.

Create a SSH keypair on your web server with no passphrase.

In a valid pet configuritation file on the web server, define a backend for
each Puppet server::

  [default]
  backend.puppet1 = ssh puppet@puppet1.example.com
  backend.puppet2 = ssh puppet@puppet2.example.com

If you only have one Puppet server you can omit the backend name::

  [default]
  backend = ssh -i /path/to/private_key puppet@puppet.example.com

Now on your Puppet servers as a user who has access to modify Puppet files,
install the public key from the web server into ``~/.ssh/authorized_keys`` and
force run the ``pet cgi-backend`` subcommand::

  command="/path/to/pet cgi-backend $SSH_ORIGINAL_COMMAND" ssh-rsa AAAAAasdafsdwehasdwa23ra232...

Also make sure to include a valid pet configuritation file on your Puppet
servers which defines at least the remote git URL::

  [default]
  remote = git@bitbucket.org:MyGroup/puppet-repo.git

Now when you push to your master repository, your Puppet servers will pull the
latest commits into their environments.
