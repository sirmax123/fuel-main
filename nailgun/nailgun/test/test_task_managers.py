# -*- coding: utf-8 -*-
import json
import time

from mock import patch

from nailgun.settings import settings

import nailgun
import nailgun.rpc as rpc
from nailgun.task.manager import DeploymentTaskManager
from nailgun.task.fake import FAKE_THREADS
from nailgun.task.errors import WrongNodeStatus
from nailgun.test.base import BaseHandlers
from nailgun.test.base import reverse
from nailgun.api.models import Cluster, Attributes, Task, Notification, Node


class TestTaskManagers(BaseHandlers):

    def tearDown(self):
        # wait for fake task thread termination
        import threading
        for thread in threading.enumerate():
            if thread is not threading.currentThread():
                if hasattr(thread, "rude_join"):
                    timer = time.time()
                    timeout = 25
                    thread.rude_join(timeout)
                    if time.time() - timer > timeout:
                        raise Exception(
                            '{0} seconds is not enough'
                            ' - possible hanging'.format(
                                timeout
                            )
                        )

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_deployment_task_managers(self):
        cluster = self.create_cluster_api()
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         status="discover",
                                         pending_addition=True)
        node2 = self.create_default_node(cluster_id=cluster['id'],
                                         status="ready",
                                         pending_addition=True)
        node3 = self.create_default_node(cluster_id=cluster['id'],
                                         pending_deletion=True)
        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        self.assertEquals(200, resp.status)
        response = json.loads(resp.body)
        supertask_uuid = response['uuid']
        supertask = self.db.query(Task).filter_by(
            uuid=supertask_uuid
        ).first()
        self.assertEquals(supertask.name, 'deploy')
        self.assertIn(supertask.status, ('running', 'ready'))
        self.assertEquals(len(supertask.subtasks), 2)

        timer = time.time()
        timeout = 10
        while True:
            self.db.refresh(node1)
            self.db.refresh(node2)
            if node1.status in ('provisioning', 'provisioned') and \
                    node2.status == 'provisioned':
                break
            if time.time() - timer > timeout:
                raise Exception("Something wrong with the statuses")
            time.sleep(1)

        timer = time.time()
        timeout = 60
        while supertask.status == 'running':
            self.db.refresh(supertask)
            if time.time() - timer > timeout:
                raise Exception("Deployment seems to be hanged")
            time.sleep(1)
        self.db.refresh(node1)
        self.db.refresh(node2)
        self.assertEquals(node1.status, 'ready')
        self.assertEquals(node2.status, 'ready')
        self.assertEquals(node1.progress, 100)
        self.assertEquals(node2.progress, 100)
        self.assertEquals(supertask.status, 'ready')
        self.assertEquals(supertask.progress, 100)
        self.assertEquals(supertask.message, (
            "Successfully removed 1 node(s). No errors occured; "
            "Deployment of installation '{0}' is done").format(
                cluster['name']))

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_deployment_fails_if_node_offline(self):
        cluster = self.create_cluster_api()
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         role="controller",
                                         status="offline",
                                         name="Offline node",
                                         pending_addition=True)
        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        response = json.loads(resp.body)
        supertask_uuid = response['uuid']
        supertask = self.db.query(Task).filter_by(
            uuid=supertask_uuid
        ).first()
        timer = time.time()
        timeout = 60
        while supertask.status == 'running':
            self.db.refresh(supertask)
            if time.time() - timer > timeout:
                raise Exception("Deployment seems to be hanged")
            time.sleep(1)
        self.assertEqual(supertask.status, 'error')
        self.assertEqual(
            supertask.message,
            u"Deployment has failed:\n'Offline node': Node is offline"
        )

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_redeployment_works(self):
        cluster = self.create_cluster_api(mode="ha")
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         role="controller",
                                         pending_addition=True)
        node2 = self.create_default_node(cluster_id=cluster['id'],
                                         role="compute",
                                         pending_addition=True)
        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )

        response = json.loads(resp.body)
        supertask_uuid = response['uuid']
        supertask = self.db.query(Task).filter_by(
            uuid=supertask_uuid
        ).first()

        timer = time.time()
        timeout = 60
        while supertask.status == 'running':
            self.db.refresh(supertask)
            if time.time() - timer > timeout:
                raise Exception("First deployment seems to be hanged")
            time.sleep(1)
        self.db.refresh(node1)
        self.db.refresh(node2)
        self.assertEquals(node1.status, 'ready')
        self.assertEquals(node2.status, 'ready')
        self.assertEquals(node1.progress, 100)
        self.assertEquals(node2.progress, 100)
        self.assertEquals(supertask.status, 'ready')
        self.assertEquals(supertask.progress, 100)

        node3 = self.create_default_node(cluster_id=cluster['id'],
                                         role="controller",
                                         pending_addition=True)

        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        response = json.loads(resp.body)
        supertask_uuid = response['uuid']
        supertask = self.db.query(Task).filter_by(
            uuid=supertask_uuid
        ).first()

        timer = time.time()
        timeout = 60
        while supertask.status == 'running':
            self.db.refresh(supertask)
            if time.time() - timer > timeout:
                raise Exception("Second deployment seems to be hanged")
            time.sleep(1)
        self.db.refresh(node1)
        self.db.refresh(node2)
        self.db.refresh(node3)
        self.assertEquals(node1.status, 'ready')
        self.assertEquals(node2.status, 'ready')
        self.assertEquals(node3.status, 'ready')
        self.assertEquals(node1.progress, 100)
        self.assertEquals(node2.progress, 100)
        self.assertEquals(node3.progress, 100)
        self.assertEquals(supertask.status, 'ready')
        self.assertEquals(supertask.progress, 100)

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_redeployment_error_nodes(self):
        cluster = self.create_cluster_api(mode="ha")
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         role="controller",
                                         status="error",
                                         error_type="provision",
                                         error_msg="Test Error",
                                         pending_addition=True)
        node2 = self.create_default_node(cluster_id=cluster['id'],
                                         role="compute",
                                         pending_addition=True)
        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        response = json.loads(resp.body)
        supertask_uuid = response['uuid']
        supertask = self.db.query(Task).filter_by(
            uuid=supertask_uuid
        ).first()

        timer = time.time()
        timeout = 60
        while supertask.status == 'running':
            self.db.refresh(supertask)
            if time.time() - timer > timeout:
                raise Exception("First deployment seems to be hanged")
            time.sleep(1)

        self.db.refresh(node1)
        self.db.refresh(node2)
        self.assertEquals(node1.status, 'error')
        self.assertEquals(node1.needs_reprovision, True)
        self.assertEquals(node2.status, 'provisioned')
        self.assertEquals(supertask.status, 'error')
        self.assertEquals(supertask.progress, 100)
        notif_node = self.db.query(Notification).filter_by(
            topic="error",
            message=u"Failed to deploy node '{0}': {1}".format(
                node1.name,
                node1.error_msg
            )
        ).first()
        self.assertIsNotNone(notif_node)
        notif_deploy = self.db.query(Notification).filter_by(
            topic="error",
            message=u"Deployment has failed:\n'{0}': {1}".format(
                node1.name,
                node1.error_msg
            )
        ).first()
        self.assertIsNotNone(notif_deploy)
        all_notif = self.db.query(Notification).all()
        self.assertEqual(len(all_notif), 2)

        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        response = json.loads(resp.body)
        supertask_uuid = response['uuid']
        supertask = self.db.query(Task).filter_by(
            uuid=supertask_uuid
        ).first()

        timer = time.time()
        timeout = 60
        while supertask.status == 'running':
            self.db.refresh(supertask)
            if time.time() - timer > timeout:
                raise Exception("Second deployment seems to be hanged")
            time.sleep(1)

        # TODO: update test for successful deployment at the second launch
        self.assertEquals(supertask.status, 'error')
        self.assertEquals(supertask.progress, 100)

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_network_verify_task_managers(self):
        cluster = self.create_cluster_api()
        node1 = self.create_default_node(cluster_id=cluster['id'])
        node2 = self.create_default_node(cluster_id=cluster['id'])
        resp = self.app.put(
            reverse(
                'ClusterNetworksHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        self.assertEquals(200, resp.status)
        response = json.loads(resp.body)
        task_uuid = response['uuid']
        task = self.db.query(Task).filter_by(uuid=task_uuid).first()
        self.assertEquals(task.name, 'verify_networks')
        self.assertIn(task.status, ('running', 'ready'))

    def test_deletion_empty_cluster_task_manager(self):
        cluster = self.create_cluster_api()
        resp = self.app.delete(
            reverse(
                'ClusterHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        self.assertEquals(202, resp.status)

        timer = time.time()
        timeout = 15
        clstr = self.db.query(Cluster).get(cluster["id"])
        while clstr:
            time.sleep(1)
            try:
                self.db.refresh(clstr)
            except:
                break
            if time.time() - timer > timeout:
                raise Exception("Cluster deletion seems to be hanged")

        notification = self.db.query(Notification)\
            .filter(Notification.topic == "done")\
            .filter(Notification.message == "Installation '%s' and all its "
                    "nodes are deleted" % cluster["name"]).first()
        self.assertIsNotNone(notification)

        tasks = self.db.query(Task).all()
        self.assertEqual(tasks, [])

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_deletion_cluster_task_manager(self):
        cluster = self.create_cluster_api()
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         role="controller",
                                         status="ready",
                                         progress=100)
        node2 = self.create_default_node(cluster_id=cluster['id'],
                                         role="compute",
                                         status="ready",
                                         progress=100)
        node3 = self.create_default_node(cluster_id=cluster['id'],
                                         role="compute",
                                         pending_addition=True)
        nodes_ids = [node1.id, node2.id, node3.id]
        resp = self.app.delete(
            reverse(
                'ClusterHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        self.assertEquals(202, resp.status)

        timer = time.time()
        timeout = 15
        clstr = self.db.query(Cluster).get(cluster["id"])
        while clstr:
            time.sleep(1)
            try:
                self.db.refresh(clstr)
            except:
                break
            if time.time() - timer > timeout:
                raise Exception("Cluster deletion seems to be hanged")

        notification = self.db.query(Notification)\
            .filter(Notification.topic == "done")\
            .filter(Notification.message == "Installation '%s' and all its "
                    "nodes are deleted" % cluster["name"]).first()
        self.assertIsNotNone(notification)

        tasks = self.db.query(Task).all()
        self.assertEqual(tasks, [])

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_deletion_during_deployment(self):
        cluster = self.create_cluster_api()
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         pending_addition=True)
        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        deploy_uuid = json.loads(resp.body)['uuid']
        resp = self.app.delete(
            reverse(
                'ClusterHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        timeout = 120
        timer = time.time()
        while True:
            task_deploy = self.db.query(Task).filter_by(
                uuid=deploy_uuid
            ).first()
            task_delete = self.db.query(Task).filter_by(
                cluster_id=cluster['id'],
                name="cluster_deletion"
            ).first()
            if not task_delete:
                break
            self.db.expire(task_deploy)
            self.db.expire(task_delete)
            if (time.time() - timer) > timeout:
                break
            time.sleep(0.24)

        cluster_db = self.db.query(Cluster).get(cluster['id'])
        self.assertIsNone(cluster_db)

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_node_fqdn_is_assigned(self):
        cluster = self.create_cluster_api()
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         pending_addition=True)
        node2 = self.create_default_node(cluster_id=cluster['id'],
                                         pending_addition=True)
        resp = self.app.put(
            reverse(
                'ClusterChangesHandler',
                kwargs={'cluster_id': cluster['id']}),
            headers=self.default_headers
        )
        self.assertEquals(200, resp.status)
        nodes = self.db.query(Node).all()
        for node in (node1, node2):
            self.db.refresh(node)
            fqdn = "slave-%s.%s" % (node.id, settings.DNS_DOMAIN)
            self.assertEquals(fqdn, node.fqdn)

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_no_node_no_cry(self):
        cluster = self.create_cluster_api()
        rcvr = rpc.receiver.NailgunReceiver
        manager = DeploymentTaskManager(cluster["id"])
        rcvr.deploy_resp(nodes=[
            {'uid': 666, 'id': 666, 'status': 'discover'}
        ], uuid='no_freaking_way')  # and wrong task also
        self.assertRaises(WrongNodeStatus, manager.execute)

    @patch('nailgun.task.task.rpc.cast', nailgun.task.task.fake_cast)
    @patch('nailgun.task.task.settings.FAKE_TASKS', True)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT', 80)
    @patch('nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL', 1)
    def test_no_changes_no_cry(self):
        cluster = self.create_cluster_api()
        node1 = self.create_default_node(cluster_id=cluster['id'],
                                         role="controller",
                                         status="ready")
        cluster_db = self.db.query(Cluster).get(cluster["id"])
        cluster_db.clear_pending_changes()
        manager = DeploymentTaskManager(cluster["id"])
        self.assertRaises(WrongNodeStatus, manager.execute)
