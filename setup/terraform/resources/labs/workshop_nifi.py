#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Common utilities for Python scripts
"""
from nipyapi import canvas, versioning, nifi
from nipyapi.nifi.rest import ApiException

from . import *
from .utils import efm, schreg, nifireg, nifi as nf, kafka, kudu, cdsw

PG_NAME = 'Process Sensor Data'
CONSUMER_GROUP_ID = 'iot-sensor-consumer'
PRODUCER_CLIENT_ID = 'nifi-sensor-data'

_SCHEMA_URI = 'http://raw.githubusercontent.com/cloudera-labs/edge2ai-workshop/master/sensor.avsc'


def skip_cdsw():
    flag = 'SKIP_CDSW' in os.environ
    LOG.debug('SKIP_CDSW={}'.format(flag))
    return flag


def read_in_schema(uri=_SCHEMA_URI):
    if 'SCHEMA_FILE' in os.environ and os.path.exists(os.environ['SCHEMA_FILE']):
        return open(os.environ['SCHEMA_FILE']).read()
    else:
        r = requests.get(uri)
        if r.status_code == 200:
            return r.text
        raise ValueError("Unable to retrieve schema from URI, response was %s", r.status_code)


class NiFiWorkshop(AbstractWorkshop):

    @classmethod
    def workshop_id(cls):
        """Return a short string to identify the workshop."""
        return 'nifi'

    @classmethod
    def prereqs(cls):
        """
        Return a list of prereqs for this workshop. The list can contain either:
          - Strings identifying the name of other workshops that need to be setup before this one does. In
            this case all the labs of the specified workshop will be setup.
          - Tuples (String, Integer), where the String specifies the name of the workshop and Integer the number
            of the last lab of that workshop to be executed/setup.
        """
        return ['edge']

    def before_setup(self):
        self.context.root_pg, self.context.efm_pg_id, self.context.flow_id = nf.set_environment()
        self.context.skip_cdsw = skip_cdsw()

    def after_setup(self):
        nf.wait_for_data(PG_NAME)

    def teardown(self):
        root_pg, _, flow_id = nf.set_environment()

        canvas.schedule_process_group(root_pg.id, False)
        while True:
            failed = False
            for controller in canvas.list_all_controllers(root_pg.id):
                try:
                    canvas.schedule_controller(controller, False)
                    LOG.debug('Controller %s stopped.', controller.component.name)
                except ApiException as exc:
                    if exc.status == 409 and 'is referenced by' in exc.body:
                        LOG.debug('Controller %s failed to stop. Will retry later.', controller.component.name)
                        failed = True
            if not failed:
                break

        nf.delete_all(root_pg)
        efm.delete_all(flow_id)
        schreg.delete_all_schemas()
        reg_client = versioning.get_registry_client('NiFi Registry')
        if reg_client:
            versioning.delete_registry_client(reg_client)
        nifireg.delete_flows('SensorFlows')
        kudu.drop_table()
        cdsw.delete_all_model_api_keys()

    def lab1_register_schema(self):
        # Create Schema
        schreg.create_schema(
            'SensorReading', 'Schema for the data generated by the IoT sensors', read_in_schema())

    def lab2_nifi_flow(self):
        # Create a bucket in NiFi Registry to save the edge flow versions
        self.context.sensor_bucket = versioning.get_registry_bucket('SensorFlows')
        if not self.context.sensor_bucket:
            self.context.sensor_bucket = versioning.create_registry_bucket('SensorFlows')

        # Create NiFi Process Group
        self.context.reg_client = versioning.create_registry_client(
            'NiFi Registry', nifireg.get_url(), 'The registry...')
        self.context.sensor_pg = canvas.create_process_group(self.context.root_pg, PG_NAME, (330, 350))
        self.context.sensor_flow = nifireg.save_flow_ver(
            self.context.sensor_pg, self.context.reg_client, self.context.sensor_bucket,
            flow_name='SensorProcessGroup',
            comment='Enabled version control - {}'.format(self.run_id))

        # Update default SSL context controller service
        ssl_svc_name = 'Default NiFi SSL Context Service'
        if is_tls_enabled():
            props = {
                'SSL Protocol': 'TLS',
                'Truststore Type': 'JKS',
                'Truststore Filename': '/opt/cloudera/security/jks/truststore.jks',
                'Truststore Password': get_the_pwd(),
                'Keystore Type': 'JKS',
                'Keystore Filename': '/opt/cloudera/security/jks/keystore.jks',
                'Keystore Password': get_the_pwd(),
                'key-password': get_the_pwd(),
            }
            self.context.ssl_svc = canvas.get_controller(ssl_svc_name, 'name')
            if self.context.ssl_svc:
                canvas.schedule_controller(self.context.ssl_svc, False)
                self.context.ssl_svc = canvas.get_controller(ssl_svc_name, 'name')
                canvas.update_controller(self.context.ssl_svc, nifi.ControllerServiceDTO(properties=props))
                self.context.ssl_svc = canvas.get_controller(ssl_svc_name, 'name')
                canvas.schedule_controller(self.context.ssl_svc, True)
            else:
                self.context.keytab_svc = nf.create_controller(
                    self.context.root_pg,
                    'org.apache.nifi.ssl.StandardRestrictedSSLContextService',
                    props, True,
                    name=ssl_svc_name)

        # Create controller services
        if is_tls_enabled():
            self.context.ssl_svc = canvas.get_controller(ssl_svc_name, 'name')
            props = {
                'Kerberos Keytab': '/keytabs/admin.keytab',
                'Kerberos Principal': 'admin',
            }
            self.context.keytab_svc = nf.create_controller(
                self.context.sensor_pg,
                'org.apache.nifi.kerberos.KeytabCredentialsService',
                props,
                True)
        else:
            self.context.ssl_svc = None
            self.context.keytab_svc = None

        props = {
            'url': schreg.get_api_url(),
        }
        if is_tls_enabled():
            props.update({
                'kerberos-credentials-service': self.context.keytab_svc.id,
                'ssl-context-service': self.context.ssl_svc.id,
            })
        self.context.sr_svc = nf.create_controller(
            self.context.sensor_pg, 'org.apache.nifi.schemaregistry.hortonworks.HortonworksSchemaRegistry',
            props,
            True)
        self.context.json_reader_svc = nf.create_controller(
            self.context.sensor_pg, 'org.apache.nifi.json.JsonTreeReader',
            {
                'schema-access-strategy': 'schema-name',
                'schema-registry': self.context.sr_svc.id
            },
            True)
        self.context.json_writer_svc = nf.create_controller(
            self.context.sensor_pg, 'org.apache.nifi.json.JsonRecordSetWriter',
            {
                'schema-access-strategy': 'schema-name',
                'schema-registry': self.context.sr_svc.id,
                'Schema Write Strategy': 'hwx-schema-ref-attributes'
            },
            True)
        self.context.avro_writer_svc = nf.create_controller(
            self.context.sensor_pg, 'org.apache.nifi.avro.AvroRecordSetWriter',
            {
                'schema-access-strategy': 'schema-name',
                'schema-registry': self.context.sr_svc.id,
                'Schema Write Strategy': 'hwx-content-encoded-schema'
            },
            True)

        # Create flow
        sensor_port = canvas.create_port(self.context.sensor_pg.id, 'INPUT_PORT', 'Sensor Data', 'STOPPED', (0, 0))

        upd_attr = nf.create_processor(self.context.sensor_pg, 'Set Schema Name',
                                          'org.apache.nifi.processors.attributes.UpdateAttribute', (0, 100),
                                          {
                                              'properties': {
                                                  'schema.name': 'SensorReading',
                                              },
                                          })
        canvas.create_connection(sensor_port, upd_attr)

        props = {
            'topic': 'iot',
            'record-reader': self.context.json_reader_svc.id,
            'record-writer': self.context.json_writer_svc.id,
        }
        props.update(kafka.get_common_client_properties(
            self.context, 'producer', CONSUMER_GROUP_ID, PRODUCER_CLIENT_ID))
        pub_kafka = nf.create_processor(
            self.context.sensor_pg, 'Publish to Kafka topic: iot',
            ['org.apache.nifi.processors.kafka.pubsub.PublishKafkaRecord_2_6',
             'org.apache.nifi.processors.kafka.pubsub.PublishKafkaRecord_2_0'],
            (0, 300),
            {
                'properties': props,
                'autoTerminatedRelationships': ['success'],
            })
        canvas.create_connection(upd_attr, pub_kafka, ['success'])

        fail_funnel = nf.create_funnel(self.context.sensor_pg.id, (600, 343))
        canvas.create_connection(pub_kafka, fail_funnel, ['failure'])

        # Commit changes
        nifireg.save_flow_ver(self.context.sensor_pg, self.context.reg_client, self.context.sensor_bucket,
                              flow_id=self.context.sensor_flow.version_control_information.flow_id,
                              comment='First version - {}'.format(self.run_id))

        # Start flow
        canvas.schedule_process_group(self.context.root_pg.id, True)

        # Update "from Gateway" input port to connect to the process group
        nf.update_connection(self.context.from_gw, self.context.temp_funnel, sensor_port)
        canvas.schedule_components(self.context.root_pg.id, True, [sensor_port])

    def lab4_rest_and_kudu(self):
        # Prepare Impala/Kudu table
        kudu.create_table()
        kudu_table_name = kudu.get_kudu_table_name('default', 'sensors')

        # Set required variables
        if not self.context.skip_cdsw:
            # Set the variable with the CDSW access key
            canvas.update_variable_registry(self.context.sensor_pg, [('cdsw.access.key', cdsw.get_model_access_key())])
            # Set the variable with the CDSW model API key
            canvas.update_variable_registry(self.context.sensor_pg, [('cdsw.model.api.key', cdsw.create_model_api_key())])

        # Create controllers
        self.context.json_reader_with_schema_svc = nf.create_controller(
            self.context.sensor_pg,
            'org.apache.nifi.json.JsonTreeReader',
            {
                'schema-access-strategy': 'hwx-schema-ref-attributes',
                'schema-registry': self.context.sr_svc.id
            },
            True,
            name='JsonTreeReader - With schema identifier')
        props = {
            'rest-lookup-url': cdsw.get_model_endpoint_url(),
            'rest-lookup-record-reader': self.context.json_reader_svc.id,
            'rest-lookup-record-path': '/response',
            'Authorization': 'Bearer ${cdsw.model.api.key}',
        }
        if is_tls_enabled():
            props.update({
                'rest-lookup-ssl-context-service': self.context.ssl_svc.id,
            })
        rest_lookup_svc = nf.create_controller(self.context.sensor_pg,
                                                  'org.apache.nifi.lookup.RestLookupService',
                                                  props,
                                                  True)

        # Build flow
        fail_funnel = nf.create_funnel(self.context.sensor_pg.id, (1400, 340))

        props = {
            'topic': 'iot',
            'topic_type': 'names',
            'record-reader': self.context.json_reader_with_schema_svc.id,
            'record-writer': self.context.json_writer_svc.id,
        }
        props.update(kafka.get_common_client_properties(
            self.context, 'consumer', CONSUMER_GROUP_ID, PRODUCER_CLIENT_ID))
        consume_kafka = nf.create_processor(
            self.context.sensor_pg, 'Consume Kafka iot messages',
            ['org.apache.nifi.processors.kafka.pubsub.ConsumeKafkaRecord_2_6',
             'org.apache.nifi.processors.kafka.pubsub.ConsumeKafkaRecord_2_0'],
            (700, 0),
            {'properties': props})
        canvas.create_connection(consume_kafka, fail_funnel, ['parse.failure'])

        predict = nf.create_processor(
            self.context.sensor_pg, 'Predict machine health',
            'org.apache.nifi.processors.standard.LookupRecord', (700, 200),
            {
                'properties': {
                    'record-reader': self.context.json_reader_with_schema_svc.id,
                    'record-writer': self.context.json_writer_svc.id,
                    'lookup-service': rest_lookup_svc.id,
                    'result-record-path': '/response',
                    'routing-strategy': 'route-to-success',
                    'result-contents': 'insert-entire-record',
                    'mime.type': "toString('application/json', 'UTF-8')",
                    'request.body':
                        "concat('{\"accessKey\":\"', '${cdsw.access.key}', "
                        "'\",\"request\":{\"feature\":\"', /sensor_0, ', ', "
                        "/sensor_1, ', ', /sensor_2, ', ', /sensor_3, ', ', "
                        "/sensor_4, ', ', /sensor_5, ', ', /sensor_6, ', ', "
                        "/sensor_7, ', ', /sensor_8, ', ', /sensor_9, ', ', "
                        "/sensor_10, ', ', /sensor_11, '\"}}')",
                    'request.method': "toString('post', 'UTF-8')",
                },
            })
        canvas.create_connection(predict, fail_funnel, ['failure'])
        canvas.create_connection(consume_kafka, predict, ['success'])

        update_health = nf.create_processor(
            self.context.sensor_pg, 'Update health flag',
            'org.apache.nifi.processors.standard.UpdateRecord', (700, 400),
            {
                'properties': {
                    'record-reader': self.context.json_reader_with_schema_svc.id,
                    'record-writer': self.context.json_writer_svc.id,
                    'replacement-value-strategy': 'record-path-value',
                    '/is_healthy': '/response/result',
                },
            })
        canvas.create_connection(update_health, fail_funnel, ['failure'])
        canvas.create_connection(predict, update_health, ['success'])

        write_kudu = nf.create_processor(
            self.context.sensor_pg, 'Write to Kudu', 'org.apache.nifi.processors.kudu.PutKudu',
            (700, 600),
            {
                'properties': {
                    'Kudu Masters': get_hostname() + ':7051',
                    'Table Name': kudu_table_name,
                    'record-reader': self.context.json_reader_with_schema_svc.id,
                    'kerberos-credentials-service': self.context.keytab_svc.id
                    if is_tls_enabled() else None,
                },
            })
        canvas.create_connection(write_kudu, fail_funnel, ['failure'])
        canvas.create_connection(update_health, write_kudu, ['success'])

        props = {
            'topic': 'iot_enriched',
            'record-reader': self.context.json_reader_with_schema_svc.id,
            'record-writer': self.context.json_writer_svc.id,
        }
        props.update(kafka.get_common_client_properties(
            self.context, 'producer', CONSUMER_GROUP_ID, PRODUCER_CLIENT_ID))
        pub_kafka_enriched = nf.create_processor(
            self.context.sensor_pg, 'Publish to Kafka topic: iot_enriched',
            ['org.apache.nifi.processors.kafka.pubsub.PublishKafkaRecord_2_6',
             'org.apache.nifi.processors.kafka.pubsub.PublishKafkaRecord_2_0'],
            (300, 600),
            {
                'properties': props,
                'autoTerminatedRelationships': ['success', 'failure'],
            })
        canvas.create_connection(update_health, pub_kafka_enriched, ['success'])

        props = {
            'topic': 'iot_enriched_avro',
            'record-reader': self.context.json_reader_with_schema_svc.id,
            'record-writer': self.context.avro_writer_svc.id,
        }
        props.update(kafka.get_common_client_properties(
            self.context, 'producer', CONSUMER_GROUP_ID, PRODUCER_CLIENT_ID))
        pub_kafka_enriched_avro = nf.create_processor(
            self.context.sensor_pg, 'Publish to Kafka topic: iot_enriched_avro',
            ['org.apache.nifi.processors.kafka.pubsub.PublishKafkaRecord_2_6',
             'org.apache.nifi.processors.kafka.pubsub.PublishKafkaRecord_2_0'],
            (-100, 600),
            {
                'properties': props,
                'autoTerminatedRelationships': ['success', 'failure'],
            })
        canvas.create_connection(update_health, pub_kafka_enriched_avro, ['success'])

        monitor_activity = nf.create_processor(
            self.context.sensor_pg, 'Monitor Activity',
            'org.apache.nifi.processors.standard.MonitorActivity', (700, 800),
            {
                'properties': {
                    'Threshold Duration': '45 secs',
                    'Continually Send Messages': 'true',
                },
                'autoTerminatedRelationships': ['activity.restored', 'success'],
            })
        canvas.create_connection(monitor_activity, fail_funnel, ['inactive'])
        canvas.create_connection(write_kudu, monitor_activity, ['success'])

        # Version flow
        nifireg.save_flow_ver(self.context.sensor_pg, self.context.reg_client, self.context.sensor_bucket,
                              flow_id=self.context.sensor_flow.version_control_information.flow_id,
                              comment='Second version - {}'.format(self.run_id))

        # Start everything
        canvas.schedule_process_group(self.context.root_pg.id, True)
