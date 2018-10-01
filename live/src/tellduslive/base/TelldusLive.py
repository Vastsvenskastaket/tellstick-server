# -*- coding: utf-8 -*-

import bz2
import logging
import random
from StringIO import StringIO
import struct
import threading
import time

from pbkdf2 import PBKDF2
from Crypto.Cipher import AES
from Crypto import Random
import netifaces
import requests

from base import \
	Application, \
	Settings, \
	IInterface, \
	ISignalObserver, \
	ObserverCollection, \
	Plugin, \
	implements, \
	mainthread, \
	signal, \
	slot
from board import Board
from .ServerList import ServerList
from .ServerConnection import ServerConnection
from .LiveMessage import LiveMessage

class ITelldusLiveObserver(IInterface):
	def liveConnected():
		"""This method is called when we have succesfully connected to a Live! server"""
	def liveRegistered(params):
		"""This method is called when we have succesfully registered with a Live! server"""
	def liveDisconnected():
		"""This method is call when we are disconnected"""

class TelldusLive(Plugin):
	implements(ISignalObserver)
	observers = ObserverCollection(ITelldusLiveObserver)

	def __init__(self):
		logging.info("Telldus Live! loading")
		self.email = ''
		self.supportedMethods = 0
		self.connected = False
		self.registered = False
		self.running = False
		self.serverList = ServerList()
		self.lastBackedUpConfig = None
		Application().registerShutdown(self.stop)
		self.settings = Settings('tellduslive.config')
		self.uuid = self.settings['uuid']
		self.conn = ServerConnection()
		self.pingTimer = 0
		self.thread = threading.Thread(target=self.run)
		if self.conn.publicKey != '':
			# Only connect if the keys has been set.
			self.thread.start()

	@slot('configurationWritten')
	def configurationWritten(self, path):
		if self.lastBackedUpConfig is not None:
			self.lastBackedUpConfig = time.time()
			return
		uploadPath = 'https://%s/upload/config' % Board.liveServer()
		with open(path, 'rb') as fd:
			fileData = fd.read()
		fileData = bz2.compress(fileData)  # Compress it
		fileData = TelldusLive.deviceSpecificEncrypt(fileData)  # Encrypt it
		requests.post(
			uploadPath,
			data={'mac': TelldusLive.getMacAddr(Board.networkInterface())},
			files={'Telldus.conf.bz2': fileData}
		)

	@mainthread
	def handleMessage(self, message):
		if (message.name() == "notregistered"):
			self.email = ''
			self.connected = True
			self.registered = False
			params = message.argument(0).dictVal
			self.settings['uuid'] = params['uuid'].stringVal
			logging.info(
				"This client isn't activated, please activate it using this url:\n%s",
				params['url'].stringVal
			)
			self.observers.liveConnected()
			return

		if (message.name() == "registered"):
			self.connected = True
			self.registered = True
			data = message.argument(0).toNative()
			if 'email' in data:
				self.email = data['email']
			self.observers.liveRegistered(data)
			return

		if (message.name() == "command"):
			# Extract ACK and handle it
			args = message.argument(0).dictVal
			if 'ACK' in args:
				msg = LiveMessage("ACK")
				msg.append(args['ACK'].intVal)
				self.send(msg)

		if (message.name() == "pong"):
			return

		if (message.name() == "disconnect"):
			self.conn.close()
			self.__disconnected()
			return

		handled = False
		for observer in self.observers:
			for func in getattr(observer, '_telldusLiveHandlers', {}).get(message.name(), []):
				func(observer, message)
				handled = True
		if not handled:
			logging.warning("Did not understand: %s", message.toByteArray())

	def isConnected(self):
		return self.connected

	def isRegistered(self):
		return self.registered

	def run(self):
		self.running = True

		wait = 0
		pongTimer, self.pingTimer = (0, 0)
		while self.running:
			if wait > 0:
				wait = wait - 1
				time.sleep(1)
				continue
			state = self.conn.process()
			if state == ServerConnection.CLOSED:
				server = self.serverList.popServer()
				if not server:
					wait = random.randint(60, 300)
					logging.warning("No servers found, retry in %i seconds", wait)
					continue
				if not self.conn.connect(server['address'], int(server['port'])):
					wait = random.randint(60, 300)
					logging.warning("Could not connect, retry in %i seconds", wait)

			elif state == ServerConnection.CONNECTED:
				pongTimer, self.pingTimer = (time.time(), time.time())
				self.__sendRegisterMessage()

			elif state == ServerConnection.MSG_RECEIVED:
				msg = self.conn.popMessage()
				if msg is None:
					continue
				pongTimer = time.time()
				self.handleMessage(msg)

			elif state == ServerConnection.DISCONNECTED:
				wait = random.randint(10, 50)
				logging.warning("Disconnected, reconnect in %i seconds", wait)
				self.__disconnected()

			else:
				if (time.time() - pongTimer >= 360):  # No pong received
					self.conn.close()
					wait = random.randint(10, 50)
					logging.warning("No pong received, disconnecting. Reconnect in %i seconds", wait)
					self.__disconnected()
				elif (time.time() - self.pingTimer >= 120):
					# Time to ping
					self.conn.send(LiveMessage("Ping"))
					self.pingTimer = time.time()

	def stop(self):
		self.running = False

	def send(self, message):
		self.conn.send(message)
		self.pingTimer = time.time()

	def pushToWeb(self, module, action, data):
		msg = LiveMessage("sendToWeb")
		msg.append(module)
		msg.append(action)
		msg.append(data)
		self.send(msg)

	def __disconnected(self):
		self.email = ''
		self.connected = False
		self.registered = False
		def sendNotification():
			self.observers.liveDisconnected()
		# Syncronize signal with main thread
		Application().queue(sendNotification)

	@staticmethod
	def handler(message):
		def call(func):
			import sys
			frame = sys._getframe(1)  # pylint: disable=W0212
			frame.f_locals.setdefault('_telldusLiveHandlers', {}).setdefault(message, []).append(func)
			return func
		return call

	def __sendRegisterMessage(self):
		print("Send register")
		msg = LiveMessage('Register')
		msg.append({
			'key': self.conn.publicKey,
			'mac': TelldusLive.getMacAddr(Board.networkInterface()),
			'secret': Board.secret(),
			'hash': 'sha1'
		})
		msg.append({
			'protocol': 3,
			'version': Board.firmwareVersion(),
			'os': 'linux',
			'os-version': 'telldus'
		})
		self.conn.send(msg)

	@staticmethod
	def getMacAddr(ifname):
		addrs = netifaces.ifaddresses(ifname)
		try:
			mac = addrs[netifaces.AF_LINK][0]['addr']
		except (IndexError, KeyError) as __error:
			return ''
		return mac.upper().replace(':', '')

	@staticmethod
	def deviceSpecificEncrypt(payload):
		# TODO: Use security plugin once available
		password = Board.secret()
		iv = Random.new().read(16)  # pylint: disable=C0103
		key = PBKDF2(password, iv).read(32)
		encryptor = AES.new(key, AES.MODE_CBC, iv)

		buff = StringIO()
		buff.write(struct.pack('<Q', len(payload)))
		buff.write(iv)
		if len(payload) % 16 != 0:
			# Pad payload
			payload += ' ' * (16 - len(payload) % 16)
		buff.write(encryptor.encrypt(payload))
		return buff.getvalue()
