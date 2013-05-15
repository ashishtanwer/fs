import sys

from socket import IPPROTO_TCP, IPPROTO_UDP, IPPROTO_ICMP
import logging
from ipaddr import IPv4Address

from pox.datapaths.switch import SoftwareSwitch
from pox.openflow import libopenflow_01 as oflib
import pox.lib.packet as pktlib
from pox.lib.addresses import *
import pox.openflow.of_01 as ofcore
from fslib.openflow import load_pox_component
import pox.core

from fslib.node import Node, PortInfo
from fslib.link import NullLink
from fslib.common import fscore, get_logger
from fslib.flowlet import Flowlet, FlowIdent
from fslib.util import default_ip_to_macaddr
from fslib.configurator import FsConfigurator

from pytricia import PyTricia

class UnhandledPoxPacketFlowletTranslation(Exception):
    pass


'''Because 'bridge' sounds better than 'monkeypatch'.'''

class PoxFlowlet(Flowlet):
    __slots__ = ['origpkt']
    def __init__(self, ident):
        Flowlet.__init__(self, ident)
        self.origpkt = None

class OpenflowMessage(Flowlet):
    __slots__ = ['ofmsg']

    def __init__(self, ident, ofmsg):
        Flowlet.__init__(self, ident)
        self.ofmsg = ofmsg
        self.bytes = len(ofmsg)

def flowlet_to_packet(flowlet):
    '''Translate an fs flowlet to a POX packet'''
    if hasattr(flowlet, "origpkt"):
        return getattr(flowlet, "origpkt")

    ident = flowlet.ident.key

    etherhdr = pktlib.ethernet()
    etherhdr.src = EthAddr(flowlet.srcmac)
    etherhdr.dst = EthAddr(flowlet.dstmac)
    etherhdr.type = pktlib.ethernet.IP_TYPE

    ipv4 = pktlib.ipv4() 
    ipv4.srcip = IPAddr(ident.srcip)
    ipv4.dstip = IPAddr(ident.dstip)
    ipv4.protocol = ident.ipproto
    ipv4.tos = flowlet.iptos
    ipv4.iplen = flowlet.bytes / flowlet.pkts

    etherhdr.payload = ipv4

    if ident.ipproto == IPPROTO_ICMP:
        layer4 = pktlib.icmp()
        layer4.type = ident.dport >> 8
        layer4.code = ident.dport & 0x00FF
    elif ident.ipproto == IPPROTO_UDP:
        layer4 = pktlib.udp()
        layer4.srcport = ident.sport 
        layer4.dstport = ident.dport 
    elif ident.ipproto == IPPROTO_TCP:
        layer4 = pktlib.tcp()
        layer4.srcport = ident.sport 
        layer4.dstport = ident.dport 
        layer4.flags = flowlet.tcpflags
    else:
        raise UnhandledPoxPacketFlowletTranslation("Can't translate IP protocol {} from flowlet to POX packet".format(fident.ipproto))
    ipv4.payload = layer4
    layer4.payload = str(flowlet)
    etherhdr.origflet = flowlet
    return etherhdr

def packet_to_flowlet(pkt):
    '''Translate a POX packet to an fs flowlet'''
    try:
        return getattr(pkt, "origflet")
    except AttributeError,e:
        log = get_logger()
        flet = None
        ip = pkt.find('ipv4')
        if ip is None:
            flet = PoxFlowlet(FlowIdent())
            log.debug("Received non-IP packet {} from POX: there's no direct translation to fs".format(str(pkt.payload)))
        else:
            dport = sport = tcpflags = 0
            if ip.protocol == IPPROTO_TCP:
                tcp = ip.payload
                sport = tcp.srcport
                dport = tcp.dstport
                tcpflags = tcp.flags
                log.debug("Translating POX TCP packet to fs {}".format(tcp))
            elif ip.protocol == IPPROTO_UDP:
                udp = ip.payload
                sport = udp.srcport
                dport = udp.dstport
                log.debug("Translating POX UDP packet to fs {}".format(udp))
            elif ip.protocol == IPPROTO_ICMP:
                icmp = ip.payload
                dport = (icmp.type << 8) | icmp.code
                log.debug("Translating POX ICMP packet to fs {}".format(icmp))
            else:
                log.warn("Received unhandled IPv4 packet {} from POX: can't translate to fs".format(str(ip.payload)))

            flet = PoxFlowlet(FlowIdent(srcip=ip.srcip, dstip=ip.dstip, ipproto=ip.protocol, sport=sport, dport=dport))
            flet.tcpflags = tcpflags
            flet.iptos = ip.tos

        flet.srcmac = pkt.src
        flet.dstmac = pkt.dst
        flet.pkts = 1
        flet.bytes = len(pkt)
        flet.origpkt = pkt
        return flet

class PoxBridgeSoftwareSwitch(SoftwareSwitch):
    def _output_packet_physical(self, packet, port_num):
        self.forward(packet, port_num)
        SoftwareSwitch._output_packet_physical(self, packet, port_num)

    def set_output_packet_callback(self, fn):
        self.forward = fn

class OpenflowSwitch(Node):
    __slots__ = ['dpid', 'pox_switch', 'controller_name', 'controller_links', 'ipdests', 'interface_to_port_map', 'trafgen_ip']

    def __init__(self, name, measurement_config, **kwargs):
        Node.__init__(self, name, measurement_config, **kwargs)
        self.dpid = abs(hash(name))
        self.pox_switch = PoxBridgeSoftwareSwitch(self.dpid, name=name, 
            ports=0, miss_send_len=2**16, max_buffers=2**8, features=None)
        self.pox_switch.set_connection(self)
        self.pox_switch.set_output_packet_callback(self. send_packet)
        self.controller_name = kwargs.get('controller', 'controller')
        self.controller_links = {}
        self.interface_to_port_map = {}

        self.ipdests = PyTricia()
        for prefix in kwargs.get('ipdests','').split():
            self.ipdests[prefix] = True

        # explicitly add a localhost link/interface
        ipa,ipb = [ ip for ip in next(FsConfigurator.link_subnetter).iterhosts() ]
        self.add_link(NullLink, ipa, ipb, 'remote', remotemac=default_ip_to_macaddr(ipb))
        self.trafgen_ip = str(ipa)

    def send_packet(self, packet, port_num):
        '''Forward a data plane packet out a given port'''
        flet = packet_to_flowlet(packet)
        pinfo = self.ports[port_num]
        # self.logger.debug("Switch sending translated packet {}->{} on port {} to {}".format(packet, flet, port_num, pinfo.link.egress_name))
        flet.srcmac,flet.dstmac = pinfo.localmac,pinfo.remotemac
        pinfo.link.flowlet_arrival(flet, self.name, pinfo.remoteip)

    def send(self, ofmessage):
        '''Callback function for POX SoftwareSwitch to send an outgoing OF message
        to controller.'''
        if not self.started:
            self.logger.debug("OF switch-to-controller deferred message {}".format(ofmessage))
            evid = 'deferred switch->controller send'
            fscore().after(0.0, evid, self.send, ofmessage)
        else:
            self.logger.debug("OF switch-to-controller {} - {}".format(str(self.controller_links[self.controller_name]), ofmessage))
            clink = self.controller_links[self.controller_name]
            self.controller_links[self.controller_name].flowlet_arrival(OpenflowMessage(FlowIdent(), ofmessage), self.name, self.controller_name)

    def set_message_handler(self, *args):
        '''Dummy callback function for POX SoftwareSwitchBase'''
        pass

    def process_packet(self, poxpkt, inputport):
        '''Process an incoming POX packet.  Mainly want to check whether
        it's an ARP and update our ARP "table" state'''
        self.logger.debug("Switch {} processing packet: {}".format(self.name, str(poxpkt)))
        if poxpkt.type == poxpkt.ARP_TYPE:
            if poxpkt.payload.opcode == pktlib.arp.REQUEST:
                self.logger.debug("Got ARP request: {}".format(str(poxpkt.payload)))
                arp = poxpkt.payload
                dstip = str(IPv4Address(arp.protodst))
                srcip = str(IPv4Address(arp.protosrc))
                if dstip in self.interface_to_port_map:
                    portnum = self.interface_to_port_map[dstip]
                    pinfo = self.ports[portnum]
                    if not pinfo.remotemac:
                        self.logger.debug("Learned MAC/IP mapping {}->{}".format(arp.hwsrc,srcip))
                        pinfo = PortInfo(pinfo.link, pinfo.localip, pinfo.remoteip, pinfo.localmac, str(arp.hwsrc))
                        self.ports[portnum] = pinfo


    def flowlet_arrival(self, flowlet, prevnode, destnode, input_intf=None):
        '''Incoming flowlet: determine whether it's a data plane flowlet or whether it's an OF message
        coming back from the controller'''
        if input_intf is None:
            input_intf = self.trafgen_ip

        if isinstance(flowlet, OpenflowMessage):
            self.logger.debug("Received from controller: {}".format(flowlet.ofmsg))
            ofmsg = None
            if isinstance(flowlet.ofmsg, oflib.ofp_base):
                ofmsg = flowlet.ofmsg
            elif isinstance(flowlet.ofmsg, str):
                ofhdr = oflib.ofp_header()
                ofhdr.unpack(flowlet.ofmsg)
                ofmsg = oflib._message_type_to_class[ofhdr.header_type]()
                ofmsg.unpack(flowlet.ofmsg)
                self.pox_switch.rx_message(self, ofmsg)
            else:
                raise UnhandledPoxPacketFlowletTranslation("Not an openflow message from controller: {}".format(flowlet.ofmsg))

            self.pox_switch.rx_message(self, ofmsg)


        elif isinstance(flowlet, PoxFlowlet):
            self.logger.debug("Received PoxFlowlet: {}".format(str(flowlet.origpkt)))
            input_port = self.interface_to_port_map[input_intf]
            self.process_packet(flowlet.origpkt, input_port)
            self.pox_switch.rx_packet(flowlet.origpkt, input_port)

        elif isinstance(flowlet, Flowlet):
            input_port = self.interface_to_port_map[input_intf]
            portinfo = self.ports[input_port]
            # self.logger.info("Received flowlet in {} intf{} dstmac{} plocal{}  --  {}".format(self.name, input_intf, flowlet.dstmac, portinfo.localmac, type(flowlet)))

            if portinfo.link is NullLink:
                flowlet.srcmac,flowlet.dstmac = portinfo.remotemac,portinfo.localmac
                # self.logger.warn("Local flowlet: rewriting MAC addresses {}".format(portinfo))
            elif flowlet.dstmac != portinfo.localmac:
                self.logger.debug("Arriving flowlet dstmac does not match input port (probably flooded) {} {}".format(flowlet.dstmac, portinfo))

            self.measure_flow(flowlet, prevnode, input_intf)
            # assume this is an incoming flowlet on the dataplane.  
            # reformat it and inject it into the POX switch
            self.logger.debug("Flowlet arrival in OF switch {} {} {} {} {}".format(self.name, flowlet.dstaddr, prevnode, destnode, input_intf))
            if self.ipdests.get(flowlet.dstaddr, None):
                # FIXME: autoack
                self.logger.debug("Flowlet arrived at destination {}".format(self.name))
            else:                
                pkt = flowlet_to_packet(flowlet)
                pkt.flowlet = flowlet
                self.pox_switch.rx_packet(pkt, input_port)
        else:
            raise UnhandledPoxPacketFlowletTranslation("Unexpected message in OF switch: {}".format(type(flowlet)))

    def add_link(self, link, localip, remoteip, next_node, remotemac=''):
        localip = str(localip)
        remoteip = str(remoteip)
        if next_node == self.controller_name:
            self.logger.debug("Adding link to {}: {}".format(self.name, link))
            self.controller_links[self.controller_name] = link
        else:
            portnum = len(self.ports)+1
            self.pox_switch.add_port(portnum)
            ofport = self.pox_switch.ports[portnum]
            # let pox create local hw_addr and just use it
            localmac = str(ofport.hw_addr)
            pi = PortInfo(link, localip, remoteip, localmac, remotemac)
            self.ports[portnum] = pi
            self.node_to_port_map[next_node].append(portnum)
            self.interface_to_port_map[localip] = portnum
            self.logger.debug("New port in OF switch {}: {}".format(portnum, pi))

    def send_gratuitous_arps(self):
        '''Send ARPs for our own interfaces to each connected node'''
        for pnum,pinfo in self.ports.iteritems():
            # construct an ARP request for one of our known interfaces.
            # controller isn't included in any of these ports, so these
            # are only ports connected to other switches
            arp = pktlib.arp()
            arp.opcode = pktlib.arp.REQUEST 
            arp.hwsrc = pinfo.localmac
            arp.protosrc = int(IPv4Address(pinfo.localip))
            arp.protodst = int(IPv4Address(pinfo.remoteip))
            ethernet = pktlib.ethernet()
            ethernet.dst = pktlib.ETHER_BROADCAST
            ethernet.src = pinfo.localmac
            ethernet.payload = arp
            ethernet.type = ethernet.ARP_TYPE
            flet = packet_to_flowlet(ethernet)
            pinfo.link.flowlet_arrival(flet, self.name, pinfo.link.egress_node_name)

    def start(self):
        Node.start(self)
        fscore().after(0.010, "arp {}".format(self.name), self.send_gratuitous_arps)

class OpenflowController(Node):
    __slots__ = ['components', 'switch_links']

    def __init__(self, name, measurement_config, **kwargs):
        Node.__init__(self, name, measurement_config, **kwargs)
        self.components = kwargs.get('components','').split()
        self.switch_links = {}

    def flowlet_arrival(self, flowlet, prevnode, destnode, input_port="127.0.0.1"):
        '''Handle switch-to-controller incoming messages'''
        # assumption: flowlet is an OpenflowMessage
        assert(isinstance(flowlet,OpenflowMessage))
        self.switch_links[prevnode][0].simrecv(flowlet.ofmsg) 

    def add_link(self, link, hostip, remoteip, next_node):
        '''don't do much except create queue of switch connections so that we can
        eventually build ofcore.Connection objects for each one
        once start() gets called.'''
        xconn = ofcore.Connection(-1, self.controller_to_switch, next_node, link.egress_node.dpid)
        self.switch_links[next_node] = (xconn, link)

    def controller_to_switch(self, switchname, mesg):
        '''Ferry an OF message from controller to switch'''
        if not self.started:
            self.logger.debug("OF controller-to-switch deferred message {}".format(mesg))
            evid = 'deferred controller->switch send'
            fscore().after(0, evid, self.controller_to_switch, switchname, mesg)
        else:
            self.logger.debug("OF controller-to-switch {}->{}: {}".format(self.name, switchname, mesg))
            link = self.switch_links[switchname][1]
            link.flowlet_arrival(OpenflowMessage(FlowIdent(), mesg), self.name, switchname)
       
    def start(self):
        '''Load POX controller components'''
        Node.start(self)
        for component in self.components:
            self.logger.debug("Starting OF Controller Component {}".format(component))
            load_pox_component(component)

