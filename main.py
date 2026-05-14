# -*- coding: utf-8 -*-
import json
import os
import re
import time
from datetime import datetime
from typing import Optional, Tuple, Dict, List

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent

from astrbot.api.star import Context, Star
from astrbot.api.message_components import Reply, Image
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

_PLUGIN_NAME = "astrbot_plugin_group_guardian"

_POLITICAL_WHITELIST = {
    "服务器", "管理", "管理员", "管里", "官方", "维护", "系统", "系统公告",
    "客服", "运营", "测试", "服务管理", "客户服务",
    "admin", "administrator", "system", "master",
    "client", "server", "test",
    "乱伦", "爱滋", "爱滋病", "艾滋",
    "草",
}

SWEAR_PATTERNS = [
    r'妈[的得]',
    r'(?:你|他|她|它|尼|伱)[妈馬嗎玛]',
    r'(?:傻|蠢|笨|白痴|弱智|脑残|智障|sb|s\W*b|煞笔|傻逼|沙比|煞逼|二货)',
    r'(?:操|草|艹|肏|日|干)(?:你|他|她|泥|拟|尼|呢|ma|吗|嘛)',
    r'(?:fuck|f[u*]ck|bitch|shit|damn|asshole)',
    r'(?:贱|骚|浪|婊|妓|鸡|鸭)(?:人|货|子|逼|B|b)',
    r'(?:去死|滚蛋|滚粗|gun|死全家|死妈|死爹)',
    r'(?:cnm|cao|nmsl|草泥马|艹尼玛|草拟吗|你妈死了|nmslese)',
    r'(?:婊子|贱人|骚货|烂货|荡妇|破鞋|野鸡)',
    r'(?:狗日的|狗东西|狗屎|垃圾|辣鸡|乐色)',
    r'(?:废物|废柴|fw|five|拉胯)',
    r'(?:杂种|杂碎|畜生|禽兽|狗娘养的|龟孙)',
    r'(?:你算老几|你配吗|你个.{0,5}东西)',
]

AD_PATTERNS = [
    r'(?:[加+]+\s*(?:v|V|薇|微|q|Q|扣|企鹅|群)\s*[:：]?\s*\d{5,})',
    r'(?:[vV薇微qQ扣]\s*[:：]?\s*\d{6,})',
    r'(?:加\s*(?:q|Q|扣|企鹅|好友)\s*(?:[:：])?\s*(?:获取|拿|买|免费|白嫖|领))',
    r'(?:免费.*(?:资源|福利|看片|视频|网站|网址|链接|领取|赠送|外挂|辅助|脚本|插件|软件|教程))',
    r'(?:[日天].{0,3}(?:赚|入|收|结)\s*\d{3,})',
    r'(?:[躺轻].{0,3}(?:赚|钱|收|月入|日入|过万|万元))',
    r'(?:兼职.{0,5}(?:日结|时薪|月入|高薪|在家|手机))',
    r'(?:全(?:网|套|新|场).{0,3}(?:最低价|白菜价|批发价|成本价|折扣|优惠))',
    r'(?:出(?:售|货|租).{0,3}(?:源码|软件|脚本|插件|账号|帐号|外挂|辅助))',
    r'(?:代(?:刷|充|练|挂|打|跑|做|开发|写|购|抢|抢票))',
    r'(?:(?:全网|最低|超低|跳楼).{0,3}(?:价格|价位|报价|折扣|甩卖|清仓))',
    r'(?:http[s]?://(?!.*(?:qq\.com|tencent\.com|qpic\.cn|bilibili\.com))[^\s]{4,})',
    r'(?:扫码.{0,5}(?:领取|下载|注册|加入|进群|加群|关注))',
    r'(?:点击.{0,5}(?:链接|网址|这里|下方|查看|了解更多|进群|下载))',
    r'(?:(?:诚招|招募|招收|招聘).{0,3}(?:代理|加盟|分销|合伙人|兼职|宝妈))',
    r'(?:专业.{0,3}(?:推广|引流|获客|涨粉|爆单|营销|代运营))',
    r'(?:精准.{0,3}(?:粉丝|流量|客户|获客|数据|资源))',
    r'(?:打造.{0,3}(?:爆款|爆文|爆粉|万人团队|团队))',
    r'(?:日入过[千百万元]|月入过万|年入百万|躺赚|睡后收入)',
    r'(?:(?:买|卖|要|收|求|获取|白嫖)\s*(?:外挂|辅助|脚本|科技|挂|飞天|透视|自瞄))',
    r'(?:找我\s*(?:拿|买|领|要)\s*(?:外挂|辅助|脚本|科技|挂))',
    r'(?:\d{1,2}\+\s*(?:进|加|入)\s*(?:群|裙|q|Q|扣|微|v|V))',
    r'(?:进\s*(?:群|裙)\s*\d{1,2}\+|\d{1,2}\+\s*(?:群|裙))',
    r'(?:看片\s*(?:加|＋|加q|加Q|加微|加v)|(?:加|＋)\s*(?:q|Q|微|v)\s*看片)',
    r'(?:福利\s*(?:群|裙)|(?:群|裙)\s*福利)',
    r'(?:黄\s*(?:片|图|群|裙)|(?:片|图)\s*黄)',
    r'(?:约\s*(?:炮|啪)|(?:炮|啪)\s*约)',
    r'(?:裸\s*(?:聊|照|视)|(?:聊|照|视)\s*裸)',
    r'(?:性\s*(?:服务|交易|陪)|(?:服务|交易|陪)\s*性)',
    r'(?:18\+\s*\d{5,})',
    r'(?:\d{1,2}\+\s*\d{5,})',
    r'(?:18\+\s*(?:进|加|入|群|裙|q|Q|扣|微|v|V))',
    r'(?:成\s*(?:人|人)|(?:人)\s*成)',
    r'(?:成\s*(?:年|年)|(?:年)\s*成)',
    r'(?:未\s*(?:成年|成)|(?:成)\s*未)',
    r'(?:禁\s*(?:区|区)|(?:区)\s*禁)',
    r'(?:限\s*(?:制级|制)|(?:制)\s*限)',
    r'(?:r\s*(?:18|18))',
    r'(?:ns\s*(?:fw|fw))',
    r'(?:里\s*(?:番|番)|(?:番)\s*里)',
    r'(?:肉\s*(?:番|番)|(?:番)\s*肉)',
    r'(?:无\s*(?:码|码)|(?:码)\s*无)',
    r'(?:有\s*(?:码|码)|(?:码)\s*有)',
    r'(?:高\s*(?:清|清)|(?:清)\s*高)',
    r'(?:超\s*(?:清|清)|(?:清)\s*超)',
    r'(?:蓝\s*(?:光|光)|(?:光)\s*蓝)',
    r'(?:4\s*(?:k|k))',
    r'(?:1080\s*(?:p|p))',
    r'(?:720\s*(?:p|p))',
    r'(?:收\s*(?:徒|徒)|(?:徒)\s*收)',
    r'(?:带\s*(?:人|人)|(?:人)\s*带)',
    r'(?:招\s*(?:人|人)|(?:人)\s*招)',
    r'(?:招\s*(?:生|生)|(?:生)\s*招)',
    r'(?:招\s*(?:代理|代理)|(?:代理)\s*招)',
    r'(?:招\s*(?:徒弟|徒弟)|(?:徒弟)\s*招)',
    r'(?:学\s*(?:技术|技术)|(?:技术)\s*学)',
    r'(?:教\s*(?:技术|技术)|(?:技术)\s*教)',
    r'(?:传\s*(?:技术|技术)|(?:技术)\s*传)',
    r'(?:培\s*(?:训|训)|(?:训)\s*培)',
    r'(?:课\s*(?:程|程)|(?:程)\s*课)',
    r'(?:教\s*(?:程|程)|(?:程)\s*教)',
    r'(?:项\s*(?:目|目)|(?:目)\s*项)',
    r'(?:合\s*(?:作|作)|(?:作)\s*合)',
    r'(?:加\s*(?:盟|盟)|(?:盟)\s*加)',
    r'(?:入\s*(?:伙|伙)|(?:伙)\s*入)',
    r'(?:跟\s*(?:我|我)|(?:我)\s*跟)',
    r'(?:带\s*(?:你|你)|(?:你)\s*带)',
    r'(?:投\s*(?:放|放)|(?:放)\s*投)',
    r'(?:惊\s*(?:喜|喜)|(?:喜)\s*惊)',
    r'(?:福\s*(?:利|利)|(?:利)\s*福)',
    r'(?:挂\s*(?:圈|圈)|(?:圈)\s*挂)',
    r'(?:端\s*(?:圈|圈)|(?:圈)\s*端)',
    r'(?:黑\s*(?:产|产)|(?:产)\s*黑)',
    r'(?:灰\s*(?:产|产)|(?:产)\s*灰)',
    r'(?:跑\s*(?:路|路)|(?:路)\s*跑)',
    r'(?:圈\s*(?:钱|钱)|(?:钱)\s*圈)',
    r'(?:割\s*(?:韭菜|韭菜)|(?:韭菜)\s*割)',
    r'(?:收\s*(?:割|割)|(?:割)\s*收)',
    r'(?:韭\s*(?:菜|菜)|(?:菜)\s*韭)',
    r'(?:普\s*(?:通|通)|(?:通)\s*普)',
    r'(?:亲\s*(?:传|传)|(?:传)\s*亲)',
    r'(?:拜\s*(?:师|师)|(?:师)\s*拜)',
    r'(?:师\s*(?:傅|傅)|(?:傅)\s*师)',
    r'(?:师\s*(?:父|父)|(?:父)\s*师)',
    r'(?:出\s*(?:师|师)|(?:师)\s*出)',
    r'(?:学\s*(?:徒|徒)|(?:徒)\s*学)',
    r'(?:徒\s*(?:弟|弟)|(?:弟)\s*徒)',
    r'(?:开\s*(?:户|卡|户头)|(?:户|卡)\s*开)',
    r'(?:银\s*(?:行卡|行)|(?:行)\s*银)',
    r'(?:电\s*(?:话卡|话)|(?:话)\s*电)',
    r'(?:支\s*(?:付宝|付)|(?:付)\s*支)',
    r'(?:微\s*(?:信|信)|(?:信)\s*微)',
    r'(?:跑\s*(?:分|分)|(?:分)\s*跑)',
    r'(?:洗\s*(?:钱|钱)|(?:钱)\s*洗)',
    r'(?:刷\s*(?:流水|单|信誉|好评)|(?:流水|单|信誉|好评)\s*刷)',
    r'(?:资\s*(?:金过桥|金)|(?:金)\s*资)',
    r'(?:虚\s*(?:拟货币|拟币|拟)|(?:拟货币|拟币|拟)\s*虚)',
    r'(?:区\s*(?:块链|块)|(?:块)\s*区)',
    r'(?:挖\s*(?:矿|矿)|(?:矿)\s*挖)',
    r'(?:博\s*(?:彩|彩)|(?:彩)\s*博)',
    r'(?:赌\s*(?:博|博)|(?:博)\s*赌)',
    r'(?:投\s*(?:注|注)|(?:注)\s*投)',
    r'(?:彩\s*(?:票|票)|(?:票)\s*彩)',
    r'(?:网\s*(?:贷|贷)|(?:贷)\s*网)',
    r'(?:校\s*(?:园贷|园)|(?:园)\s*校)',
    r'(?:裸\s*(?:贷|贷)|(?:贷)\s*裸)',
    r'(?:高\s*(?:利贷|利)|(?:利)\s*高)',
    r'(?:信\s*(?:用卡|用)|(?:用)\s*信)',
    r'(?:套\s*(?:现|现)|(?:现)\s*套)',
    r'(?:代\s*(?:还|还)|(?:还)\s*代)',
    r'(?:提\s*(?:额|额)|(?:额)\s*提)',
    r'(?:养\s*(?:卡|卡)|(?:卡)\s*养)',
    r'(?:过\s*(?:账|账)|(?:账)\s*过)',
    r'(?:走\s*(?:账|账)|(?:账)\s*走)',
    r'(?:对\s*(?:公户|公)|(?:公)\s*对)',
    r'(?:个\s*(?:体户|体)|(?:体)\s*个)',
    r'(?:营\s*(?:业执照|业)|(?:业)\s*营)',
    r'(?:公\s*(?:司注册|司)|(?:司)\s*公)',
    r'(?:代\s*(?:实名|实名|认证|注册)|(?:实名|认证|注册)\s*代)',
    r'(?:实\s*(?:名制|名)|(?:名)\s*实)',
    r'(?:人\s*(?:脸识别|脸)|(?:脸)\s*人)',
    r'(?:手\s*(?:机号|机)|(?:机)\s*手)',
    r'(?:银\s*(?:行账户|行账)|(?:行账|行)\s*银)',
    r'(?:支\s*(?:付账户|付账)|(?:付账|付)\s*支)',
    r'(?:第\s*(?:三方支付|三方)|(?:三方)\s*第)',
    r'(?:收\s*(?:款码|款)|(?:款)\s*收)',
    r'(?:转\s*(?:账|账)|(?:账)\s*转)',
    r'(?:洗\s*(?:白|白)|(?:白)\s*洗)',
    r'(?:黑\s*(?:钱|钱)|(?:钱)\s*黑)',
    r'(?:灰\s*(?:产|产)|(?:产)\s*灰)',
    r'(?:薅\s*(?:羊毛|羊毛)|(?:羊毛)\s*薅)',
    r'(?:撸\s*(?:口子|口子)|(?:口子)\s*撸)',
    r'(?:空\s*(?:手套白狼|手套)|(?:手套)\s*空)',
    r'(?:骗\s*(?:贷|贷)|(?:贷)\s*骗)',
    r'(?:诈\s*(?:骗|骗)|(?:骗)\s*诈)',
    r'(?:电\s*(?:信诈骗|信诈)|(?:信诈|信)\s*电)',
    r'(?:网\s*(?:络诈骗|络诈)|(?:络诈|络)\s*网)',
    r'(?:刷\s*(?:单兼职|单兼)|(?:单兼|单)\s*刷)',
    r'(?:网\s*(?:赚|赚)|(?:赚)\s*网)',
    r'(?:快\s*(?:钱|钱)|(?:钱)\s*快)',
    r'(?:暴\s*(?:利|利)|(?:利)\s*暴)',
    r'(?:投\s*(?:资理财|资理)|(?:资理|资)\s*投)',
    r'(?:高\s*(?:回报|回报)|(?:回报)\s*高)',
    r'(?:零\s*(?:风险|风险)|(?:风险)\s*零)',
    r'(?:保\s*(?:本|本)|(?:本)\s*保)',
    r'(?:稳\s*(?:赚|赚)|(?:赚)\s*稳)',
    r'(?:内\s*(?:幕消息|幕消)|(?:幕消|幕)\s*内)',
    r'(?:庄\s*(?:家|家)|(?:家)\s*庄)',
    r'(?:操\s*(?:盘|盘)|(?:盘)\s*操)',
    r'(?:控\s*(?:盘|盘)|(?:盘)\s*控)',
    r'(?:拉\s*(?:升|升)|(?:升)\s*拉)',
    r'(?:割\s*(?:韭菜|韭菜)|(?:韭菜)\s*割)',
    r'(?:接\s*(?:盘|盘)|(?:盘)\s*接)',
    r'(?:多\s*(?:空|空)|(?:空)\s*多)',
    r'(?:杠\s*(?:杆|杆)|(?:杆)\s*杠)',
    r'(?:配\s*(?:资|资)|(?:资)\s*配)',
    r'(?:融\s*(?:券|券)|(?:券)\s*融)',
    r'(?:期\s*(?:货|货)|(?:货)\s*期)',
    r'(?:外\s*(?:汇|汇)|(?:汇)\s*外)',
    r'(?:现\s*(?:货|货)|(?:货)\s*现)',
    r'(?:黄\s*(?:金|金)|(?:金)\s*黄)',
    r'(?:原\s*(?:油|油)|(?:油)\s*原)',
    r'(?:白\s*(?:银|银)|(?:银)\s*白)',
    r'(?:数\s*(?:字货币|字货)|(?:字货|字)\s*数)',
    r'(?:代\s*(?:币|币)|(?:币)\s*代)',
    r'(?:空\s*(?:投|投)|(?:投)\s*空)',
    r'(?:矿\s*(?:机|机)|(?:机)\s*矿)',
    r'(?:矿\s*(?:池|池)|(?:池)\s*矿)',
    r'(?:算\s*(?:力|力)|(?:力)\s*算)',
    r'(?:节\s*(?:点|点)|(?:点)\s*节)',
    r'(?:钱\s*(?:包|包)|(?:包)\s*钱)',
    r'(?:私\s*(?:钥|钥)|(?:钥)\s*私)',
    r'(?:公\s*(?:钥|钥)|(?:钥)\s*公)',
    r'(?:助\s*(?:记词|记)|(?:记)\s*助)',
    r'(?:冷\s*(?:钱包|钱)|(?:钱)\s*冷)',
    r'(?:热\s*(?:钱包|钱)|(?:钱)\s*热)',
    r'(?:交\s*(?:易所|易)|(?:易)\s*交)',
    r'(?:去\s*(?:中心化|中心)|(?:中心)\s*去)',
    r'(?:defi|nft|ico|ieo|ido)',
    r'(?:智能\s*(?:合约|合)|(?:合)\s*智能)',
    r'(?:gas\s*(?:费|费)|(?:费)\s*gas)',
    r'(?:滑\s*(?:点|点)|(?:点)\s*滑)',
    r'(?:无\s*(?:常损失|常损)|(?:常损|常)\s*无)',
    r'(?:流\s*(?:动性|动)|(?:动)\s*流)',
    r'(?:质\s*(?:押|押)|(?:押)\s*质)',
    r'(?:借\s*(?:贷|贷)|(?:贷)\s*借)',
    r'(?:闪\s*(?:兑|兑)|(?:兑)\s*闪)',
    r'(?:套\s*(?:利|利)|(?:利)\s*套)',
    r'(?:搬\s*(?:砖|砖)|(?:砖)\s*搬)',
    r'(?:量\s*(?:化|化)|(?:化)\s*量)',
    r'(?:机\s*(?:器人|器)|(?:器)\s*机)',
    r'(?:脚\s*(?:本|本)|(?:本)\s*脚)',
    r'(?:挂\s*(?:机|机)|(?:机)\s*挂)',
    r'(?:抢\s*(?:单|单)|(?:单)\s*抢)',
    r'(?:秒\s*(?:杀|杀)|(?:杀)\s*秒)',
    r'(?:黄\s*(?:牛|牛)|(?:牛)\s*黄)',
    r'(?:代\s*(?:拍|拍)|(?:拍)\s*代)',
    r'(?:代\s*(?:抢|抢)|(?:抢)\s*代)',
    r'(?:代\s*(?:购|购)|(?:购)\s*代)',
    r'(?:海\s*(?:淘|淘)|(?:淘)\s*海)',
    r'(?:转\s*(?:运|运)|(?:运)\s*转)',
    r'(?:清\s*(?:关|关)|(?:关)\s*清)',
    r'(?:水\s*(?:客|客)|(?:客)\s*水)',
    r'(?:走\s*(?:私|私)|(?:私)\s*走)',
    r'(?:逃\s*(?:税|税)|(?:税)\s*逃)',
    r'(?:避\s*(?:税|税)|(?:税)\s*避)',
    r'(?:洗\s*(?:发票|发票)|(?:发票)\s*洗)',
    r'(?:虚\s*(?:开发票|开)|(?:开)\s*虚)',
    r'(?:假\s*(?:发票|发)|(?:发)\s*假)',
    r'(?:真\s*(?:发票|发)|(?:发)\s*真)',
    r'(?:增\s*(?:值税|值)|(?:值)\s*增)',
    r'(?:普\s*(?:票|票)|(?:票)\s*普)',
    r'(?:专\s*(?:票|票)|(?:票)\s*专)',
    r'(?:发\s*(?:票|票)|(?:票)\s*发)',
    r'(?:报\s*(?:销|销)|(?:销)\s*报)',
    r'(?:抵\s*(?:扣|扣)|(?:扣)\s*抵)',
    r'(?:进\s*(?:项|项)|(?:项)\s*进)',
    r'(?:出\s*(?:项|项)|(?:项)\s*出)',
    r'(?:对\s*(?:公|公)|(?:公)\s*对)',
    r'(?:私\s*(?:户|户)|(?:户)\s*私)',
    r'(?:个\s*(?:人|人)|(?:人)\s*个)',
    r'(?:企\s*(?:业|业)|(?:业)\s*企)',
    r'(?:商\s*(?:户|户)|(?:户)\s*商)',
    r'(?:收\s*(?:单|单)|(?:单)\s*收)',
    r'(?:二\s*(?:维码|维)|(?:维)\s*二)',
    r'(?:条\s*(?:形码|形)|(?:形)\s*条)',
    r'(?:扫\s*(?:码|码)|(?:码)\s*扫)',
    r'(?:收\s*(?:款|款)|(?:款)\s*收)',
    r'(?:付\s*(?:款|款)|(?:款)\s*付)',
    r'(?:转\s*(?:让|让)|(?:让)\s*转)',
    r'(?:出\s*(?:租|租)|(?:租)\s*出)',
    r'(?:借\s*(?:用|用)|(?:用)\s*借)',
    r'(?:租\s*(?:借|借)|(?:借)\s*租)',
    r'(?:共\s*(?:享|享)|(?:享)\s*共)',
    r'(?:合\s*(?:租|租)|(?:租)\s*合)',
    r'(?:分\s*(?:租|租)|(?:租)\s*分)',
    r'(?:转\s*(?:租|租)|(?:租)\s*转)',
    r'(?:承\s*(?:租|租)|(?:租)\s*承)',
    r'(?:二\s*(?:房东|房)|(?:房)\s*二)',
    r'(?:中\s*(?:介|介)|(?:介)\s*中)',
    r'(?:代\s*(?:理|理)|(?:理)\s*代)',
    r'(?:经\s*(?:纪|纪)|(?:纪)\s*经)',
    r'(?:挂\s*(?:靠|靠)|(?:靠)\s*挂)',
    r'(?:托\s*(?:管|管)|(?:管)\s*托)',
    r'(?:代\s*(?:管|管)|(?:管)\s*代)',
    r'(?:保\s*(?:管|管)|(?:管)\s*保)',
    r'(?:存\s*(?:管|管)|(?:管)\s*存)',
    r'(?:资\s*(?:金|金)|(?:金)\s*资)',
    r'(?:现\s*(?:金|金)|(?:金)\s*现)',
    r'(?:现\s*(?:流|流)|(?:流)\s*现)',
    r'(?:流\s*(?:水|水)|(?:水)\s*流)',
    r'(?:日\s*(?:结|结)|(?:结)\s*日)',
    r'(?:周\s*(?:结|结)|(?:结)\s*周)',
    r'(?:月\s*(?:结|结)|(?:结)\s*月)',
    r'(?:季\s*(?:结|结)|(?:结)\s*季)',
    r'(?:年\s*(?:结|结)|(?:结)\s*年)',
    r'(?:分\s*(?:红|红)|(?:红)\s*分)',
    r'(?:佣\s*(?:金|金)|(?:金)\s*佣)',
    r'(?:提\s*(?:成|成)|(?:成)\s*提)',
    r'(?:返\s*(?:点|点)|(?:点)\s*返)',
    r'(?:返\s*(?:利|利)|(?:利)\s*返)',
    r'(?:折\s*(?:扣|扣)|(?:扣)\s*折)',
    r'(?:优\s*(?:惠|惠)|(?:惠)\s*优)',
    r'(?:补\s*(?:贴|贴)|(?:贴)\s*补)',
    r'(?:津\s*(?:贴|贴)|(?:贴)\s*津)',
    r'(?:奖\s*(?:金|金)|(?:金)\s*奖)',
    r'(?:福\s*(?:利|利)|(?:利)\s*福)',
    r'(?:工\s*(?:资|资)|(?:资)\s*工)',
    r'(?:薪\s*(?:资|资)|(?:资)\s*薪)',
    r'(?:报\s*(?:酬|酬)|(?:酬)\s*报)',
    r'(?:劳\s*(?:务|务)|(?:务)\s*劳)',
    r'(?:人\s*(?:工|工)|(?:工)\s*人)',
    r'(?:手\s*(?:续费|续)|(?:续)\s*手)',
    r'(?:服\s*(?:务费|务)|(?:务)\s*服)',
    r'(?:管\s*(?:理费|理)|(?:理)\s*管)',
    r'(?:咨\s*(?:询费|询)|(?:询)\s*咨)',
    r'(?:顾\s*(?:问费|问)|(?:问)\s*顾)',
    r'(?:技\s*(?:术费|术)|(?:术)\s*技)',
    r'(?:培\s*(?:训费|训)|(?:训)\s*培)',
    r'(?:教\s*(?:育费|育)|(?:育)\s*教)',
    r'(?:会\s*(?:员费|员)|(?:员)\s*会)',
    r'(?:加\s*(?:盟费|盟)|(?:盟)\s*加)',
    r'(?:保\s*(?:证金|证)|(?:证)\s*保)',
    r'(?:押\s*(?:金|金)|(?:金)\s*押)',
    r'(?:定\s*(?:金|金)|(?:金)\s*定)',
    r'(?:订\s*(?:金|金)|(?:金)\s*订)',
    r'(?:违\s*(?:约金|约)|(?:约)\s*违)',
    r'(?:赔\s*(?:偿金|偿)|(?:偿)\s*赔)',
    r'(?:损\s*(?:失费|失)|(?:失)\s*损)',
    r'(?:罚\s*(?:款|款)|(?:款)\s*罚)',
    r'(?:滞\s*(?:纳金|纳)|(?:纳)\s*滞)',
    r'(?:利\s*(?:息|息)|(?:息)\s*利)',
    r'(?:罚\s*(?:息|息)|(?:息)\s*罚)',
    r'(?:复\s*(?:利|利)|(?:利)\s*复)',
    r'(?:单\s*(?:利|利)|(?:利)\s*单)',
    r'(?:年\s*(?:化|化)|(?:化)\s*年)',
    r'(?:日\s*(?:息|息)|(?:息)\s*日)',
    r'(?:月\s*(?:息|息)|(?:息)\s*月)',
    r'(?:季\s*(?:息|息)|(?:息)\s*季)',
    r'(?:本\s*(?:金|金)|(?:金)\s*本)',
    r'(?:本\s*(?:息|息)|(?:息)\s*本)',
    r'(?:等\s*(?:额|额)|(?:额)\s*等)',
    r'(?:等\s*(?:本|本)|(?:本)\s*等)',
    r'(?:先\s*(?:息|息)|(?:息)\s*先)',
    r'(?:后\s*(?:本|本)|(?:本)\s*后)',
    r'(?:到\s*(?:期|期)|(?:期)\s*到)',
    r'(?:逾\s*(?:期|期)|(?:期)\s*逾)',
    r'(?:冒\s*(?:用|用)|(?:用)\s*冒)',
    r'(?:盗\s*(?:用|用)|(?:用)\s*盗)',
    r'(?:伪\s*(?:造|造)|(?:造)\s*伪)',
    r'(?:变\s*(?:造|造)|(?:造)\s*变)',
    r'(?:假\s*(?:证|证)|(?:证)\s*假)',
    r'(?:伪\s*(?:证|证)|(?:证)\s*伪)',
    r'(?:假\s*(?:身份|身)|(?:身)\s*假)',
    r'(?:伪\s*(?:身份|身)|(?:身)\s*伪)',
    r'(?:冒\s*(?:身份|身)|(?:身)\s*冒)',
    r'(?:盗\s*(?:身份|身)|(?:身)\s*盗)',
    r'(?:借\s*(?:身份|身)|(?:身)\s*借)',
    r'(?:租\s*(?:身份|身)|(?:身)\s*租)',
    r'(?:买\s*(?:身份|身)|(?:身)\s*买)',
    r'(?:卖\s*(?:身份|身)|(?:身)\s*卖)',
    r'(?:收\s*(?:身份|身)|(?:身)\s*收)',
    r'(?:求\s*(?:身份|身)|(?:身)\s*求)',
    r'(?:代\s*(?:开|开)|(?:开)\s*代)',
    r'(?:代\s*(?:办|办)|(?:办)\s*代)',
    r'(?:代\s*(?:申请|申)|(?:申)\s*代)',
    r'(?:代\s*(?:注册|注)|(?:注)\s*代)',
    r'(?:代\s*(?:认证|认)|(?:认)\s*代)',
    r'(?:代\s*(?:验证|验)|(?:验)\s*代)',
    r'(?:代\s*(?:绑定|绑)|(?:绑)\s*代)',
    r'(?:代\s*(?:解绑|解)|(?:解)\s*代)',
    r'(?:代\s*(?:挂失|挂)|(?:挂)\s*代)',
    r'(?:代\s*(?:补办|补)|(?:补)\s*代)',
    r'(?:代\s*(?:激活|激)|(?:激)\s*代)',
    r'(?:代\s*(?:注销|注)|(?:注)\s*代)',
    r'(?:代\s*(?:年审|年)|(?:年)\s*代)',
    r'(?:代\s*(?:检|检)|(?:检)\s*代)',
    r'(?:代\s*(?:审|审)|(?:审)\s*代)',
    r'(?:代\s*(?:签|签)|(?:签)\s*代)',
    r'(?:代\s*(?:领|领)|(?:领)\s*代)',
    r'(?:代\s*(?:取|取)|(?:取)\s*代)',
    r'(?:代\s*(?:办|办)|(?:办)\s*代)',
    r'(?:代\s*(?:理|理)|(?:理)\s*代)',
    r'(?:代\s*(?:持|持)|(?:持)\s*代)',
    r'(?:背\s*(?:书|书)|(?:书)\s*背)',
    r'(?:挂\s*(?:名|名)|(?:名)\s*挂)',
    r'(?:借\s*(?:名|名)|(?:名)\s*借)',
    r'(?:顶\s*(?:名|名)|(?:名)\s*顶)',
    r'(?:替\s*(?:名|名)|(?:名)\s*替)',
    r'(?:假\s*(?:名|名)|(?:名)\s*假)',
    r'(?:化\s*(?:名|名)|(?:名)\s*化)',
    r'(?:匿\s*(?:名|名)|(?:名)\s*匿)',
    r'(?:虚\s*(?:名|名)|(?:名)\s*虚)',
    r'(?:冒\s*(?:名|名)|(?:名)\s*冒)',
    r'(?:套\s*(?:名|名)|(?:名)\s*套)',
    r'(?:双\s*(?:身份|身)|(?:身)\s*双)',
    r'(?:多\s*(?:身份|身)|(?:身)\s*多)',
    r'(?:第\s*(?:二身份|二身)|(?:二身|二)\s*第)',
    r'(?:备\s*(?:用身份|用身)|(?:用身|用)\s*备)',
    r'(?:隐\s*(?:藏身份|藏身)|(?:藏身|藏)\s*隐)',
    r'(?:真\s*(?:实身份|实身)|(?:实身|实)\s*真)',
    r'(?:本\s*(?:人身份|人身)|(?:人身|人)\s*本)',
    r'(?:他\s*(?:人身份|人身)|(?:人身|人)\s*他)',
    r'(?:身\s*(?:份证|份)|(?:份)\s*身)',
    r'(?:身\s*(?:份证号|份号)|(?:份号|份)\s*身)',
    r'(?:身\s*(?:份信息|份信)|(?:份信|份)\s*身)',
    r'(?:身\s*(?:份证明|份证)|(?:份证|份)\s*身)',
    r'(?:身\s*(?:份复印件|份复)|(?:份复|份)\s*身)',
    r'(?:身\s*(?:份扫描件|份扫)|(?:份扫|份)\s*身)',
    r'(?:身\s*(?:份照片|份照)|(?:份照|份)\s*身)',
    r'(?:身\s*(?:份正反面|份正)|(?:份正|份)\s*身)',
    r'(?:身\s*(?:份原件|份原)|(?:份原|份)\s*身)',
    r'(?:身\s*(?:份挂失|份挂)|(?:份挂|份)\s*身)',
    r'(?:身\s*(?:份补办|份补)|(?:份补|份)\s*身)',
    r'(?:身\s*(?:份过期|份过)|(?:份过|份)\s*身)',
    r'(?:身\s*(?:份到期|份到)|(?:份到|份)\s*身)',
    r'(?:身\s*(?:份更新|份更)|(?:份更|份)\s*身)',
    r'(?:身\s*(?:份换领|份换)|(?:份换|份)\s*身)',
    r'(?:身\s*(?:份申领|份申)|(?:份申|份)\s*身)',
    r'(?:身\s*(?:份核验|份核)|(?:份核|份)\s*身)',
    r'(?:身\s*(?:份比对|份比)|(?:份比|份)\s*身)',
    r'(?:身\s*(?:份核查|份核)|(?:份核|份)\s*身)',
    r'(?:身\s*(?:份验证|份验)|(?:份验|份)\s*身)',
    r'(?:户\s*(?:口本|口)|(?:口)\s*户)',
    r'(?:户\s*(?:口|口)|(?:口)\s*户)',
    r'(?:户\s*(?:籍|籍)|(?:籍)\s*户)',
    r'(?:暂\s*(?:住证|住)|(?:住)\s*暂)',
    r'(?:居\s*(?:住证|住)|(?:住)\s*居)',
    r'(?:护\s*(?:照|照)|(?:照)\s*护)',
    r'(?:驾\s*(?:驶证|驶)|(?:驶)\s*驾)',
    r'(?:行\s*(?:驶证|驶)|(?:驶)\s*行)',
    r'(?:军\s*(?:官证|官)|(?:官)\s*军)',
    r'(?:士\s*(?:兵证|兵)|(?:兵)\s*士)',
    r'(?:军\s*(?:人证|人)|(?:人)\s*军)',
    r'(?:残\s*(?:疾证|疾)|(?:疾)\s*残)',
    r'(?:医\s*(?:保证|保)|(?:保)\s*医)',
    r'(?:社\s*(?:保证|保)|(?:保)\s*社)',
    r'(?:养\s*(?:老证|老)|(?:老)\s*养)',
    r'(?:退\s*(?:休证|休)|(?:休)\s*退)',
    r'(?:学\s*(?:生证|生)|(?:生)\s*学)',
    r'(?:教\s*(?:师证|师)|(?:师)\s*教)',
    r'(?:记\s*(?:者证|者)|(?:者)\s*记)',
    r'(?:工\s*(?:作证|作)|(?:作)\s*工)',
    r'(?:工\s*(?:牌|牌)|(?:牌)\s*工)',
    r'(?:名\s*(?:片|片)|(?:片)\s*名)',
    r'(?:名\s*(?:册|册)|(?:册)\s*名)',
    r'(?:名\s*(?:单|单)|(?:单)\s*名)',
    r'(?:名\s*(?:录|录)|(?:录)\s*名)',
    r'(?:名\s*(?:簿|簿)|(?:簿)\s*名)',
    r'(?:名\s*(?:帖|帖)|(?:帖)\s*名)',
    r'(?:名\s*(?:刺|刺)|(?:刺)\s*名)',
    r'(?:名\s*(?:签|签)|(?:签)\s*名)',
    r'(?:名\s*(?:章|章)|(?:章)\s*名)',
    r'(?:名\s*(?:戳|戳)|(?:戳)\s*名)',
    r'(?:名\s*(?:印|印)|(?:印)\s*名)',
    r'(?:查\s*(?:身份证|身份)|(?:身份)\s*查)',
    r'(?:查\s*(?:户口|户)|(?:户)\s*查)',
    r'(?:查\s*(?:信息|信)|(?:信)\s*查)',
    r'(?:查\s*(?:资料|资)|(?:资)\s*查)',
    r'(?:查\s*(?:档案|档)|(?:档)\s*查)',
    r'(?:查\s*(?:记录|记)|(?:记)\s*查)',
    r'(?:查\s*(?:底细|底)|(?:底)\s*查)',
    r'(?:查\s*(?:背景|背)|(?:背)\s*查)',
    r'(?:查\s*(?:来历|来)|(?:来)\s*查)',
    r'(?:查\s*(?:住址|住)|(?:住)\s*查)',
    r'(?:查\s*(?:电话|电)|(?:电)\s*查)',
    r'(?:查\s*(?:手机|手)|(?:手)\s*查)',
    r'(?:查\s*(?:号码|号)|(?:号)\s*查)',
    r'(?:查\s*(?:姓名|姓)|(?:姓)\s*查)',
    r'(?:查\s*(?:年龄|年)|(?:年)\s*查)',
    r'(?:查\s*(?:生日|生)|(?:生)\s*查)',
    r'(?:查\s*(?:籍贯|籍)|(?:籍)\s*查)',
    r'(?:查\s*(?:民族|民)|(?:民)\s*查)',
    r'(?:查\s*(?:婚姻|婚)|(?:婚)\s*查)',
    r'(?:查\s*(?:学历|学)|(?:学)\s*查)',
    r'(?:查\s*(?:工作|工)|(?:工)\s*查)',
    r'(?:查\s*(?:单位|单)|(?:单)\s*查)',
    r'(?:查\s*(?:公司|公)|(?:公)\s*查)',
    r'(?:查\s*(?:房产|房)|(?:房)\s*查)',
    r'(?:查\s*(?:车辆|车)|(?:车)\s*查)',
    r'(?:查\s*(?:银行|银)|(?:银)\s*查)',
    r'(?:查\s*(?:账户|账)|(?:账)\s*查)',
    r'(?:查\s*(?:余额|余)|(?:余)\s*查)',
    r'(?:查\s*(?:流水|流)|(?:流)\s*查)',
    r'(?:查\s*(?:征信|征)|(?:征)\s*查)',
    r'(?:查\s*(?:信用|信)|(?:信)\s*查)',
    r'(?:查\s*(?:记录|记)|(?:记)\s*查)',
    r'(?:查\s*(?:案底|案)|(?:案)\s*查)',
    r'(?:查\s*(?:犯罪|犯)|(?:犯)\s*查)',
    r'(?:查\s*(?:违法|违)|(?:违)\s*查)',
    r'(?:查\s*(?:诉讼|诉)|(?:诉)\s*查)',
    r'(?:查\s*(?:执行|执)|(?:执)\s*查)',
    r'(?:查\s*(?:失信|失)|(?:失)\s*查)',
    r'(?:查\s*(?:老赖|老)|(?:老)\s*查)',
    r'(?:曝\s*(?:光|光)|(?:光)\s*曝)',
    r'(?:公\s*(?:开|开)|(?:开)\s*公)',
    r'(?:泄\s*(?:露|露)|(?:露)\s*泄)',
    r'(?:散\s*(?:布|布)|(?:布)\s*散)',
    r'(?:传\s*(?:播|播)|(?:播)\s*传)',
    r'(?:发\s*(?:布|布)|(?:布)\s*发)',
    r'(?:公\s*(?:布|布)|(?:布)\s*公)',
    r'(?:晒\s*(?:出|出)|(?:出)\s*晒)',
    r'(?:贴\s*(?:出|出)|(?:出)\s*贴)',
    r'(?:挂\s*(?:出|出)|(?:出)\s*挂)',
    r'(?:爆\s*(?:出|出)|(?:出)\s*爆)',
    r'(?:抖\s*(?:出|出)|(?:出)\s*抖)',
    r'(?:透\s*(?:露|露)|(?:露)\s*透)',
    r'(?:走\s*(?:漏|漏)|(?:漏)\s*走)',
    r'(?:泄\s*(?:密|密)|(?:密)\s*泄)',
    r'(?:窃\s*(?:取|取)|(?:取)\s*窃)',
    r'(?:偷\s*(?:取|取)|(?:取)\s*偷)',
    r'(?:盗\s*(?:取|取)|(?:取)\s*盗)',
    r'(?:非\s*(?:法获取|法获)|(?:法获|法)\s*非)',
    r'(?:侵\s*(?:犯隐私|犯隐)|(?:犯隐|犯)\s*侵)',
    r'(?:隐\s*(?:私|私)|(?:私)\s*隐)',
    r'(?:个\s*(?:人信息|人信)|(?:人信|人)\s*个)',
    r'(?:个\s*(?:人资料|人资)|(?:人资|人)\s*个)',
    r'(?:个\s*(?:人数据|人数)|(?:人数|人)\s*个)',
    r'(?:敏\s*(?:感信息|感信)|(?:感信|感)\s*敏)',
    r'(?:机\s*(?:密信息|密信)|(?:密信|密)\s*机)',
    r'(?:内\s*(?:部信息|部信)|(?:部信|部)\s*内)',
    r'(?:核\s*(?:心信息|心信)|(?:心信|心)\s*核)',
    r'(?:重\s*(?:要信息|要信)|(?:要信|要)\s*重)',
    r'(?:关\s*(?:键信息|键信)|(?:键信|键)\s*关)',
    r'(?:秘\s*(?:密|密)|(?:密)\s*秘)',
    r'(?:机\s*(?:密|密)|(?:密)\s*机)',
    r'(?:内\s*(?:幕|幕)|(?:幕)\s*内)',
    r'(?:黑\s*(?:料|料)|(?:料)\s*黑)',
    r'(?:八\s*(?:卦|卦)|(?:卦)\s*八)',
    r'(?:猛\s*(?:料|料)|(?:料)\s*猛)',
    r'(?:料\s*(?:子|子)|(?:子)\s*料)',
    r'(?:瓜\s*(?:子|子)|(?:子)\s*瓜)',
    r'(?:吃\s*(?:瓜|瓜)|(?:瓜)\s*吃)',
    r'(?:扒\s*(?:皮|皮)|(?:皮)\s*扒)',
    r'(?:人\s*(?:肉|肉)|(?:肉)\s*人)',
    r'(?:开\s*(?:盒|盒)|(?:盒)\s*开)',
    r'(?:盒\s*(?:武器|武)|(?:武)\s*盒)',
    r'(?:开\s*(?:箱|箱)|(?:箱)\s*开)',
    r'(?:开\s*(?:房记录|房记)|(?:房记|房)\s*开)',
    r'(?:开\s*(?:房|房)|(?:房)\s*开)',
    r'(?:住\s*(?:宿记录|宿记)|(?:宿记|宿)\s*住)',
    r'(?:住\s*(?:宿|宿)|(?:宿)\s*住)',
    r'(?:通\s*(?:话记录|话记)|(?:话记|话)\s*通)',
    r'(?:通\s*(?:话|话)|(?:话)\s*通)',
    r'(?:短\s*(?:信记录|信记)|(?:信记|信)\s*短)',
    r'(?:短\s*(?:信|信)|(?:信)\s*短)',
    r'(?:聊\s*(?:天记录|天记)|(?:天记|天)\s*聊)',
    r'(?:聊\s*(?:天|天)|(?:天)\s*聊)',
    r'(?:微\s*(?:信记录|信记)|(?:信记|信)\s*微)',
    r'(?:微\s*(?:信|信)|(?:信)\s*微)',
    r'(?:q\s*(?:q记录|q记)|(?:q记|q)\s*q)',
    r'(?:q\s*(?:q|q)|(?:q)\s*q)',
    r'(?:邮\s*(?:件|件)|(?:件)\s*邮)',
    r'(?:邮\s*(?:箱|箱)|(?:箱)\s*邮)',
    r'(?:跑\s*(?:路|路)|(?:路)\s*跑)',
    r'(?:收\s*(?:徒|徒)|(?:徒)\s*收)',
    r'(?:带\s*(?:人|人)|(?:人)\s*带)',
    r'(?:带\s*(?:你|你)|(?:你)\s*带)',
    r'(?:跟\s*(?:我|我)|(?:我)\s*跟)',
    r'(?:学\s*(?:技术|技术)|(?:技术)\s*学)',
    r'(?:教\s*(?:技术|技术)|(?:技术)\s*教)',
    r'(?:传\s*(?:技术|技术)|(?:技术)\s*传)',
    r'(?:挂\s*(?:圈|圈)|(?:圈)\s*挂)',
    r'(?:端\s*(?:圈|圈)|(?:圈)\s*端)',
    r'(?:黑\s*(?:产|产)|(?:产)\s*黑)',
    r'(?:灰\s*(?:产|产)|(?:产)\s*灰)',
    r'(?:圈\s*(?:钱|钱)|(?:钱)\s*圈)',
    r'(?:割\s*(?:韭菜|韭菜)|(?:韭菜)\s*割)',
    r'(?:韭\s*(?:菜|菜)|(?:菜)\s*韭)',
    r'(?:吃\s*(?:香喝辣|香喝)|(?:香喝|香)\s*吃)',
    r'(?:神\s*(?:秘惊喜|秘惊)|(?:秘惊|秘)\s*神)',
    r'(?:惊\s*(?:喜|喜)|(?:喜)\s*惊)',
    r'(?:福\s*(?:利|利)|(?:利)\s*福)',
    r'(?:普\s*(?:通|通)|(?:通)\s*普)',
    r'(?:亲\s*(?:传|传)|(?:传)\s*亲)',
    r'(?:拜\s*(?:师|师)|(?:师)\s*拜)',
    r'(?:师\s*(?:傅|傅)|(?:傅)\s*师)',
    r'(?:师\s*(?:父|父)|(?:父)\s*师)',
    r'(?:出\s*(?:师|师)|(?:师)\s*出)',
    r'(?:学\s*(?:徒|徒)|(?:徒)\s*学)',
    r'(?:徒\s*(?:弟|弟)|(?:弟)\s*徒)',
    r'(?:加\s*(?:盟|盟)|(?:盟)\s*加)',
    r'(?:入\s*(?:伙|伙)|(?:伙)\s*入)',
    r'(?:合\s*(?:作|作)|(?:作)\s*合)',
    r'(?:项\s*(?:目|目)|(?:目)\s*项)',
    r'(?:投\s*(?:放|放)|(?:放)\s*投)',
    r'(?:培\s*(?:训|训)|(?:训)\s*培)',
    r'(?:课\s*(?:程|程)|(?:程)\s*课)',
    r'(?:教\s*(?:程|程)|(?:程)\s*教)',
    r'(?:招\s*(?:人|人)|(?:人)\s*招)',
    r'(?:招\s*(?:生|生)|(?:生)\s*招)',
    r'(?:招\s*(?:代理|代理)|(?:代理)\s*招)',
    r'(?:招\s*(?:徒弟|徒弟)|(?:徒弟)\s*招)',
    r'(?:有\s*(?:意者|意)|(?:意)\s*有)',
    r'(?:需\s*(?:要|要)|(?:要)\s*需)',
    r'(?:联\s*(?:系|系)|(?:系)\s*联)',
    r'(?:咨\s*(?:询|询)|(?:询)\s*咨)',
    r'(?:了\s*(?:解|解)|(?:解)\s*了)',
]


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._sync_astrbot_admins()
        self._client = None
        _gwl = self.config.get("group_white_list", [])
        self.group_white_list = [str(g).strip() for g in (_gwl if isinstance(_gwl, list) else [_gwl]) if g]
        _gbl = self.config.get("group_black_list", [])
        self.group_black_list = [str(g).strip() for g in (_gbl if isinstance(_gbl, list) else [_gbl]) if g]
        _ubl = self.config.get("user_black_list", [])
        self.user_black_list = [str(u).strip() for u in (_ubl if isinstance(_ubl, list) else [_ubl]) if u]
        self.auto_moderate_enabled = self.config.get("auto_moderate_enabled", True)
        self._compiled_swear = [re.compile(p, re.IGNORECASE) for p in SWEAR_PATTERNS]
        self._compiled_ad = [re.compile(p, re.IGNORECASE) for p in AD_PATTERNS]
        self._lexicon = self._load_lexicon()
        self._compiled_lexicon = self._compile_lexicon()
        self._moderation_logs = self._load_logs()
        self._register_web_apis()

    def _sync_astrbot_admins(self) -> None:
        try:
            ab_config = getattr(self.context, 'astrbot_config', None)
            if not ab_config:
                return
            astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
            if not astrbot_admin_ids:
                return
            plugin_admins = self.config.get("admin_list", [])
            plugin_admins = [str(a).strip() for a in (plugin_admins if isinstance(plugin_admins, list) else [plugin_admins]) if a]
            new_admins = [a for a in astrbot_admin_ids if a not in plugin_admins]
            if new_admins:
                plugin_admins.extend(new_admins)
                self.config["admin_list"] = plugin_admins
                self._save_config_safe()
                logger.info(f"[GroupMgr] 自动同步AstrBot管理员到插件admin_list: {new_admins}")
        except Exception:
            pass

    def _save_config_safe(self) -> None:
        try:
            self.config.save_config()
        except Exception:
            logger.exception("save_config failed")

    def _logs_path(self) -> str:
        return os.path.join(self._get_plugin_dir(), "moderation_logs.json")

    def _load_logs(self) -> list:
        try:
            p = self._logs_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data[-500:]
        except Exception:
            logger.exception("load_logs failed")
        return []

    def _save_logs(self) -> None:
        try:
            p = self._logs_path()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._moderation_logs, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("save_logs failed")

    def _register_web_apis(self):
        try:
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/stats",
                self._web_stats,
                ["GET"],
                "获取群管统计信息"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/config",
                self._web_get_config,
                ["GET"],
                "获取当前配置"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/config",
                self._web_update_config,
                ["POST"],
                "更新配置"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/lexicon",
                self._web_get_lexicon,
                ["GET"],
                "获取外置词库内容"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/logs",
                self._web_get_logs,
                ["GET"],
                "获取最近审核日志"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/moderation_users",
                self._web_get_moderation_users,
                ["GET"],
                "获取被撤回用户聚合列表"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/logs/delete",
                self._web_delete_logs,
                ["POST"],
                "批量删除审核日志"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/logs/export",
                self._web_export_logs,
                ["GET"],
                "导出审核日志"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/groups",
                self._web_get_groups,
                ["GET"],
                "获取群列表"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/group_members",
                self._web_get_group_members,
                ["GET"],
                "获取群成员列表"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/whitelist/add",
                self._web_whitelist_add,
                ["POST"],
                "添加群白名单"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/whitelist/remove",
                self._web_whitelist_remove,
                ["POST"],
                "移除群白名单"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/blacklist/add",
                self._web_blacklist_add,
                ["POST"],
                "添加群黑名单"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/blacklist/remove",
                self._web_blacklist_remove,
                ["POST"],
                "移除群黑名单"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/user_blacklist/add",
                self._web_user_blacklist_add,
                ["POST"],
                "添加用户黑名单"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/user_blacklist/remove",
                self._web_user_blacklist_remove,
                ["POST"],
                "移除用户黑名单"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/admin/add",
                self._web_admin_add,
                ["POST"],
                "添加管理员"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/admin/remove",
                self._web_admin_remove,
                ["POST"],
                "移除管理员"
            )
            self.context.register_web_api(
                f"/{_PLUGIN_NAME}/today_stats",
                self._web_today_stats,
                ["GET"],
                "获取今日拦截统计"
            )
            logger.info("[GroupMgr] WebUI API 已注册")
        except Exception as e:
            logger.warning(f"[GroupMgr] 注册 WebUI API 失败: {e}")

    async def _web_stats(self):
        from quart import jsonify, request
        logs = self._moderation_logs
        today_start = int(time.time()) - (int(time.time()) % 86400)
        today_logs = [l for l in logs if l.get("ts", 0) >= today_start]
        today_blocked = sum(1 for l in today_logs if "撤回" in l.get("action", ""))
        stats = {
            "plugin_name": _PLUGIN_NAME,
            "version": "v1.8.2",
            "auto_moderate_enabled": self.auto_moderate_enabled,
            "group_white_list_count": len(self.group_white_list),
            "group_black_list_count": len(self.group_black_list),
            "user_black_list_count": len(self.user_black_list),
            "admin_list_count": len(self.config.get("admin_list", [])),
            "swear_patterns_count": len(SWEAR_PATTERNS),
            "ad_patterns_count": len(AD_PATTERNS),
            "lexicon_categories_count": len(self._lexicon),
            "lexicon_total_keywords": sum(
                len(cat.get("keywords", [])) for cat in self._lexicon.values()
            ),
            "total_logs": len(logs),
            "today_total": len(today_logs),
            "today_blocked": today_blocked,
            "today_passed": sum(1 for l in today_logs if "放行" in l.get("action", "")),
        }
        return jsonify({"status": "success", "data": stats})

    async def _web_get_config(self):
        from quart import jsonify, request
        safe_config = {}
        for k, v in self.config.items():
            if any(sk in k.lower() for sk in ("token", "secret", "password", "key")):
                if "provider_id" not in k:
                    continue
            safe_config[k] = v
        safe_config["_white_list"] = self.group_white_list
        safe_config["_black_list"] = self.group_black_list
        safe_config["_user_black_list"] = self.user_black_list
        safe_config["_admin_list"] = self.config.get("admin_list", [])
        return jsonify({"status": "success", "data": safe_config})

    async def _web_update_config(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            bool_keys = [
                "enabled", "auto_moderate_enabled", "auto_moderate_notice",
                "scan_swear", "scan_ad",
                "llm_moderation_enabled", "llm_moderation_ban",
                "lexicon_political_enabled", "lexicon_porn_enabled",
                "lexicon_violent_enabled", "lexicon_reactionary_enabled",
                "lexicon_weapons_enabled", "lexicon_corruption_enabled",
                "lexicon_illegal_url_enabled", "lexicon_other_enabled",
                "ban_enabled", "unban_enabled", "kick_enabled",
                "whole_ban_enabled", "set_card_enabled",
                "send_announcement_enabled", "delete_announcement_enabled",
                "list_announcements_enabled", "member_list_enabled",
                "set_admin_enabled", "set_group_name_enabled",
                "set_title_enabled", "banned_list_enabled",
                "join_verify_enabled", "recall_enabled",
                "essence_enabled", "group_files_enabled",
                "prompt_injection_enabled",
                "group_honor_enabled", "at_all_remain_enabled",
                "ignore_requests_enabled", "group_msg_history_enabled",
                "group_portrait_enabled", "group_sign_enabled",
            ]
            list_keys = [
                "group_white_list", "group_black_list",
                "user_black_list", "admin_list",
            ]
            int_keys = ["moderation_ban_duration"]
            str_keys = ["moderation_llm_provider_id", "ban_notice"]
            updated = []
            for key in bool_keys:
                if key in data:
                    self.config[key] = bool(data[key])
                    updated.append(key)
            for key in list_keys:
                if key in data:
                    val = data[key]
                    if isinstance(val, str):
                        val = [x.strip() for x in val.replace("，", ",").split(",") if x.strip()]
                    if isinstance(val, list):
                        self.config[key] = [str(x).strip() for x in val if x]
                        updated.append(key)
            for key in int_keys:
                if key in data:
                    try:
                        val = int(data[key])
                        if val < 60:
                            val = 60
                        elif val > 2592000:
                            val = 2592000
                        self.config[key] = val
                        updated.append(key)
                    except (ValueError, TypeError):
                        pass
            for key in str_keys:
                if key in data:
                    self.config[key] = str(data[key])
                    updated.append(key)
            if "auto_moderate_enabled" in updated:
                self.auto_moderate_enabled = bool(self.config.get("auto_moderate_enabled", True))
            if any(k.startswith("lexicon_") for k in updated):
                self._compiled_lexicon = self._compile_lexicon()
            if "group_white_list" in updated:
                _gwl = self.config.get("group_white_list", [])
                self.group_white_list = [str(g).strip() for g in (_gwl if isinstance(_gwl, list) else [_gwl]) if g]
            if "group_black_list" in updated:
                _gbl = self.config.get("group_black_list", [])
                self.group_black_list = [str(g).strip() for g in (_gbl if isinstance(_gbl, list) else [_gbl]) if g]
            if "user_black_list" in updated:
                _ubl = self.config.get("user_black_list", [])
                self.user_black_list = [str(u).strip() for u in (_ubl if isinstance(_ubl, list) else [_ubl]) if u]
            if "admin_list" in updated:
                al = self.config.get("admin_list", [])
                self.config["admin_list"] = [str(a).strip() for a in (al if isinstance(al, list) else [al]) if a]
            if "enabled" in updated:
                self.config["enabled"] = bool(self.config.get("enabled", True))
            if updated:
                self._save_config_safe()
            return jsonify({"status": "success", "updated": updated})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_lexicon(self):
        from quart import jsonify, request
        return jsonify({"status": "success", "data": self._lexicon})

    async def _web_get_logs(self):
        from quart import jsonify, request
        limit = min(int(request.args.get("limit", 50)), 200)
        logs = self._moderation_logs[-limit:]
        return jsonify({"status": "success", "data": logs})

    async def _web_get_moderation_users(self):
        from quart import jsonify, request
        logs = self._moderation_logs
        action_filter = request.args.get("action", "").strip()
        filtered = logs
        if action_filter:
            filtered = [l for l in logs if action_filter in l.get("action", "")]
        user_map = {}
        for log in filtered:
            uid = log.get("user_id", "")
            if not uid:
                continue
            if uid not in user_map:
                user_map[uid] = {
                    "user_id": uid,
                    "user_name": log.get("user_name", ""),
                    "group_id": log.get("group_id", ""),
                    "count": 0,
                    "first_time": log.get("time", ""),
                    "last_time": log.get("time", ""),
                    "records": [],
                }
            u = user_map[uid]
            u["count"] += 1
            u["last_time"] = log.get("time", "")
            u["records"].append({
                "id": log.get("id"),
                "time": log.get("time", ""),
                "ts": log.get("ts", 0),
                "group_id": log.get("group_id", ""),
                "msg_preview": log.get("msg_preview", ""),
                "msg_text": log.get("msg_text", ""),
                "action": log.get("action", ""),
                "reason": log.get("reason", ""),
            })
        users = sorted(user_map.values(), key=lambda x: x["count"], reverse=True)
        return jsonify({"status": "success", "data": users})

    async def _web_delete_logs(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", [])
            delete_all = data.get("delete_all", False)
            logs = self._moderation_logs
            if delete_all:
                self._moderation_logs = []
                self._save_logs()
                return jsonify({"status": "success", "deleted": len(logs)})
            if not ids:
                return jsonify({"status": "error", "message": "未指定要删除的日志ID"})
            id_set = set(int(i) for i in ids)
            before = len(logs)
            self._moderation_logs = [l for l in logs if l.get("id") not in id_set]
            for i, log in enumerate(self._moderation_logs):
                log["id"] = i
            self._save_logs()
            return jsonify({"status": "success", "deleted": before - len(self._moderation_logs)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_export_logs(self):
        from quart import jsonify, request
        fmt = request.args.get("format", "json").strip().lower()
        logs = self._moderation_logs
        if fmt == "csv":
            import csv, io
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["ID", "时间", "群号", "用户ID", "用户名", "消息内容", "操作", "原因"])
            for l in logs:
                writer.writerow([
                    l.get("id", ""), l.get("time", ""), l.get("group_id", ""),
                    l.get("user_id", ""), l.get("user_name", ""),
                    l.get("msg_text", ""), l.get("action", ""), l.get("reason", ""),
                ])
            return output.getvalue(), 200, {"Content-Type": "text/csv; charset=utf-8", "Content-Disposition": "attachment; filename=moderation_logs.csv"}
        return jsonify({"status": "success", "data": logs})

    async def _web_get_groups(self):
        from quart import jsonify, request
        client = await self._get_client()
        if not client:
            return jsonify({"status": "error", "message": "无法获取QQ客户端，请确保已连接"})
        try:
            result = await client.call_action('get_group_list')
            groups = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            enriched = []
            today_start = int(time.time()) - (int(time.time()) % 86400)
            for g in groups:
                gid = str(g.get("group_id", ""))
                member_count = g.get("member_count")
                if member_count is None:
                    try:
                        mlist = await client.call_action('get_group_member_list', group_id=int(gid))
                        member_count = len(mlist) if isinstance(mlist, list) else 0
                    except Exception:
                        member_count = 0
                is_white = gid in self.group_white_list
                is_black = gid in self.group_black_list
                today_count = 0
                if is_white:
                    logs = self._moderation_logs
                    today_count = sum(1 for l in logs if str(l.get("group_id", "")) == gid and l.get("ts", 0) >= today_start and "撤回" in l.get("action", ""))
                enriched.append({
                    "group_id": gid,
                    "group_name": g.get("group_name", ""),
                    "member_count": member_count,
                    "avatar": f"https://p.qlogo.cn/gh/{gid}/{gid}/",
                    "is_white": is_white,
                    "is_black": is_black,
                    "today_blocked": today_count,
                })
            return jsonify({"status": "success", "data": enriched})
        except Exception as e:
            return jsonify({"status": "error", "message": f"获取群列表失败: {e}"})

    async def _web_get_group_members(self):
        from quart import jsonify, request
        group_id = request.args.get("group_id", "").strip()
        if not group_id:
            return jsonify({"status": "error", "message": "缺少 group_id 参数"})
        client = await self._get_client()
        if not client:
            return jsonify({"status": "error", "message": "无法获取QQ客户端"})
        try:
            gid = int(group_id)
            result = await client.call_action('get_group_member_list', group_id=gid, no_cache=True)
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            enriched = []
            admin_set = set(str(a).strip() for a in self.config.get("admin_list", []) if a)
            for m in members:
                uid = str(m.get("user_id", ""))
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                role = m.get("role", "member")
                title = m.get("title", "") or m.get("special_title", "")
                is_plugin_admin = uid in admin_set
                enriched.append({
                    "user_id": uid,
                    "nickname": nickname,
                    "card": card,
                    "display_name": card or nickname,
                    "role": role,
                    "title": title,
                    "avatar": f"https://q.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640",
                    "is_plugin_admin": is_plugin_admin,
                })
            role_order = {"owner": 0, "admin": 1, "member": 2}
            enriched.sort(key=lambda x: (role_order.get(x["role"], 9), x["display_name"]))
            return jsonify({"status": "success", "data": enriched})
        except Exception as e:
            return jsonify({"status": "error", "message": f"获取群成员失败: {e}"})

    async def _web_whitelist_add(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self.group_black_list:
                self.group_black_list.remove(group_id)
                self.config["group_black_list"] = self.group_black_list
            if group_id not in self.group_white_list:
                self.group_white_list.append(group_id)
                self.config["group_white_list"] = self.group_white_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_whitelist_remove(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self.group_white_list:
                self.group_white_list.remove(group_id)
                self.config["group_white_list"] = self.group_white_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_add(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self.group_white_list:
                self.group_white_list.remove(group_id)
                self.config["group_white_list"] = self.group_white_list
            if group_id not in self.group_black_list:
                self.group_black_list.append(group_id)
                self.config["group_black_list"] = self.group_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_remove(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if group_id in self.group_black_list:
                self.group_black_list.remove(group_id)
                self.config["group_black_list"] = self.group_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_add(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            if user_id not in self.user_black_list:
                self.user_black_list.append(user_id)
                self.config["user_black_list"] = self.user_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_remove(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            if user_id in self.user_black_list:
                self.user_black_list.remove(user_id)
                self.config["user_black_list"] = self.user_black_list
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_add(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            admin_list = self.config.get("admin_list", [])
            if not isinstance(admin_list, list):
                admin_list = []
            admin_list = [str(a).strip() for a in admin_list if a]
            if user_id not in admin_list:
                admin_list.append(user_id)
                self.config["admin_list"] = admin_list
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": admin_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_remove(self):
        from quart import jsonify, request
        try:
            data = await request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            admin_list = self.config.get("admin_list", [])
            if not isinstance(admin_list, list):
                admin_list = []
            admin_list = [str(a).strip() for a in admin_list if a]
            if user_id in admin_list:
                admin_list.remove(user_id)
                self.config["admin_list"] = admin_list
            self._save_config_safe()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": admin_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_today_stats(self):
        from quart import jsonify, request
        logs = self._moderation_logs
        today_start = int(time.time()) - (int(time.time()) % 86400)
        today_logs = [l for l in logs if l.get("ts", 0) >= today_start]
        group_stats = {}
        user_stats = {}
        for l in today_logs:
            gid = str(l.get("group_id", ""))
            uid = str(l.get("user_id", ""))
            action = l.get("action", "")
            if "撤回" in action:
                if gid:
                    group_stats[gid] = group_stats.get(gid, 0) + 1
                if uid:
                    user_stats[uid] = user_stats.get(uid, 0) + 1
        group_ranking = sorted(group_stats.items(), key=lambda x: x[1], reverse=True)[:20]
        user_ranking = sorted(user_stats.items(), key=lambda x: x[1], reverse=True)[:20]
        user_names = {}
        for l in logs:
            uid = str(l.get("user_id", ""))
            if uid and uid not in user_names:
                user_names[uid] = l.get("user_name", "")
        return jsonify({
            "status": "success",
            "data": {
                "total_today": len(today_logs),
                "blocked_today": sum(1 for l in today_logs if "撤回" in l.get("action", "")),
                "passed_today": sum(1 for l in today_logs if "放行" in l.get("action", "")),
                "group_ranking": [{"group_id": g, "count": c} for g, c in group_ranking],
                "user_ranking": [{"user_id": u, "user_name": user_names.get(u, ""), "count": c} for u, c in user_ranking],
            }
        })

    def _cfg(self, key: str, default: bool = True) -> bool:
        return bool(self.config.get(key, default))

    def _safe_int(self, value, default: int = 0) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _cfg_check(self, key: str, name: str) -> Tuple[bool, str]:
        if not self._cfg("enabled"):
            return False, "插件已禁用，所有功能不可用"
        if not self._cfg(key):
            return False, f"{name}功能已在配置中禁用"
        return True, ""

    def _check_api_result(self, result, action_name: str = "操作") -> Tuple[bool, str]:
        if result is None:
            return True, ""
        if isinstance(result, dict):
            status = result.get("status", "")
            retcode = result.get("retcode", 0)
            if status == "failed" or (retcode != 0 and retcode is not None):
                msg = result.get("msg", "") or result.get("message", "") or f"错误码: {retcode}"
                return False, msg
        return True, ""

    def _get_plugin_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))

    def _load_lexicon(self) -> Dict[str, Dict]:
        lexicon_path = os.path.join(self._get_plugin_dir(), "lexicon.json")
        if not os.path.exists(lexicon_path):
            logger.warning("[GroupMgr] 外置词库文件不存在，跳过加载")
            return {}
        try:
            with open(lexicon_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            categories = data.get("categories", {})
            logger.info(f"[GroupMgr] 已加载外置词库: {len(categories)} 个分类")
            for cat_name, cat_data in categories.items():
                keywords = cat_data.get("keywords", [])
                logger.info(f"[GroupMgr]   - {cat_name}: {len(keywords)} 条关键词")
            return categories
        except Exception as e:
            logger.error(f"[GroupMgr] 加载外置词库失败: {e}")
            return {}

    def _compile_lexicon(self) -> Dict[str, List[re.Pattern]]:
        compiled = {}
        enable_political = self.config.get("lexicon_political_enabled", True)
        enable_porn = self.config.get("lexicon_porn_enabled", True)
        enable_violent = self.config.get("lexicon_violent_enabled", True)
        enable_reactionary = self.config.get("lexicon_reactionary_enabled", True)
        enable_weapons = self.config.get("lexicon_weapons_enabled", True)
        enable_corruption = self.config.get("lexicon_corruption_enabled", True)
        enable_illegal_url = self.config.get("lexicon_illegal_url_enabled", True)
        enable_other = self.config.get("lexicon_other_enabled", True)

        switch_map = {
            "political": enable_political,
            "porn": enable_porn,
            "violent_terror": enable_violent,
            "reactionary": enable_reactionary,
            "weapons": enable_weapons,
            "corruption": enable_corruption,
            "illegal_url": enable_illegal_url,
            "other": enable_other,
            "supplement": enable_other,
            "livelihood": enable_other,
            "tencent_ban": enable_other,
            "ad": True,
        }

        for cat_name, cat_data in self._lexicon.items():
            if not switch_map.get(cat_name, True):
                continue
            keywords = cat_data.get("keywords", [])
            patterns = []
            min_len = 2 if cat_name == "illegal_url" else 3
            skip_keywords = _POLITICAL_WHITELIST if cat_name == "political" else set()
            for kw in keywords:
                kw = kw.strip()
                if not kw or kw.lower() in skip_keywords:
                    continue
                if '+' in kw and cat_name != "illegal_url":
                    parts = [p.strip() for p in kw.split('+') if p.strip()]
                    for part in parts:
                        if len(part) >= min_len and part.lower() not in skip_keywords:
                            patterns.append(re.compile(re.escape(part), re.IGNORECASE))
                else:
                    if len(kw) < min_len:
                        continue
                    patterns.append(re.compile(re.escape(kw), re.IGNORECASE))
            if patterns:
                compiled[cat_name] = patterns
        return compiled

    def _check_lexicon(self, text: str) -> Dict[str, bool]:
        result = {}
        for cat_name, patterns in self._compiled_lexicon.items():
            hit = False
            for p in patterns:
                m = p.search(text)
                if m:
                    logger.info(f"[GroupMgr] 词库命中 [{cat_name}]: 关键词='{m.group()}'")
                    hit = True
                    break
            result[cat_name] = hit
        return result

    async def _get_client(self, event: AstrMessageEvent = None):
        if event:
            client = getattr(event, 'bot', None)
            if client and hasattr(client, 'call_action'):
                self._client = client
                return client
        if self._client and hasattr(self._client, 'call_action'):
            return self._client
        try:
            pm = self.context.platform_manager
            if hasattr(pm, 'get_insts'):
                platforms = pm.get_insts() or []
            else:
                platforms = pm._platforms.values() if hasattr(pm, '_platforms') else []
            for platform in platforms:
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, 'call_action'):
                        self._client = client
                        return client
                elif hasattr(platform, 'client') and hasattr(platform.client, 'call_action'):
                    self._client = platform.client
                    return platform.client
        except Exception as e:
            logger.debug(f"[GroupMgr] 从 platform_manager 获取 client 失败: {e}")
        return None

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, 'group_id') and event.group_id:
                return str(event.group_id)
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
                return str(event.message_obj.group_id)
            if hasattr(event, 'raw_message') and hasattr(event.raw_message, 'group_id'):
                return str(event.raw_message.group_id)
            gid = event.get_group_id()
            if gid:
                return str(gid)
        except Exception:
            pass
        return ""

    def _try_get_sender_id(self, event: AstrMessageEvent) -> str:
        try:
            sid = event.get_sender_id()
            if sid:
                return str(sid)
        except Exception:
            pass
        try:
            if hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                return str(event.sender.user_id)
        except Exception:
            pass
        try:
            if hasattr(event, 'user_id'):
                return str(event.user_id)
        except Exception:
            pass
        try:
            raw = getattr(event, 'raw_event', None)
            if isinstance(raw, dict):
                uid = raw.get('user_id') or raw.get('sender', {}).get('user_id')
                if uid:
                    return str(uid)
        except Exception:
            pass
        try:
            msg = getattr(event, 'message_obj', None)
            if msg and hasattr(msg, 'sender') and hasattr(msg.sender, 'user_id'):
                return str(msg.sender.user_id)
        except Exception:
            pass
        return ""

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        user_id = self._try_get_sender_id(event)
        if not user_id:
            logger.warning(f"[GroupMgr] _is_admin 无法获取user_id from {type(event).__name__}")
            return False

        astrbot_admin_ids = []
        try:
            ab_config = getattr(self.context, 'astrbot_config', None)
            if ab_config:
                astrbot_admin_ids = [str(x).strip() for x in (ab_config.get('admin_id', []) or []) if str(x).strip()]
        except Exception:
            pass
        try:
            config_admins = self.config.get("admin_list", [])
            all_admins = set(astrbot_admin_ids) | set([str(a).strip() for a in config_admins if a])
            if user_id in all_admins:
                return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 读取config admin_list失败: {e}")

        try:
            group_id = self._get_group_id(event)
            if group_id:
                try:
                    group_id_int = int(group_id)
                except (ValueError, TypeError):
                    logger.warning(f"[GroupMgr] 群号格式无效: {group_id}")
                    return False
                try:
                    user_id_int = int(user_id)
                except (ValueError, TypeError):
                    logger.warning(f"[GroupMgr] 用户ID格式无效: {user_id}")
                    return False
                client = await self._get_client(event)
                if client:
                    info = await client.call_action('get_group_member_info', group_id=group_id_int, user_id=user_id_int, no_cache=True)
                    if info:
                        role = info.get('role', '')
                        if role in ('admin', 'owner'):
                            return True
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取群成员信息失败: {e}")

        return False

    def _check_group_access(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        group_id = self._get_group_id(event)
        if not group_id:
            return True, ""
        if self.group_black_list and group_id in self.group_black_list:
            return False, f"群 {group_id} 在黑名单中"
        if self.group_white_list:
            if group_id not in self.group_white_list:
                return False, f"群 {group_id} 不在白名单中"
        return True, ""

    async def _get_image_file_from_event(self, event: AiocqhttpMessageEvent) -> Optional[str]:
        chain = event.get_messages() or []
        for seg in chain:
            if isinstance(seg, Reply):
                chain = seg.chain or []
                break
        for seg in chain:
            if isinstance(seg, Image):
                return getattr(seg, 'file', None) or getattr(seg, 'url', None) or getattr(seg, 'path', None)
        raw = getattr(event, 'message_obj', None)
        if raw:
            raw_msg = getattr(raw, 'raw_message', None)
            if raw_msg and hasattr(raw_msg, 'image') and raw_msg.image:
                return raw_msg.image
        return None

    def _truncate(self, text: str, max_chars: int = 2000) -> str:
        if len(text) <= max_chars:
            return text
        suffix = f"\n...（已截断，原{len(text)}字符）"
        limit = max_chars - len(suffix)
        if limit <= 0:
            return text[:max_chars]
        return text[:limit] + suffix

    def _format_message_content(self, raw_message) -> str:
        if raw_message is None:
            return '[空消息]'
        if not isinstance(raw_message, list):
            return str(raw_message)
        parts = []
        for seg in raw_message:
            if not isinstance(seg, dict):
                parts.append(str(seg))
                continue
            t = seg.get('type', '')
            d = seg.get('data', {}) or {}
            if t == 'text':
                parts.append(d.get('text', ''))
            elif t == 'image':
                parts.append(d.get('summary', '[图片]') or '[图片]')
            elif t == 'at':
                parts.append(f"@{d.get('qq', '')}")
            elif t == 'reply':
                parts.append(f"[回复:{d.get('id', '')}]")
            elif t == 'face':
                parts.append("[表情]")
            else:
                parts.append(f"[{t}]")
        return ''.join(parts) if parts else '[空消息]'

    def _log_moderation(self, group_id: str, user_id: str, user_name: str, msg_text: str, action: str, reason: str = ""):
        self._moderation_logs.append({
            "id": len(self._moderation_logs),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": int(time.time()),
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
            "msg_text": msg_text[:500],
            "msg_preview": msg_text[:100],
            "action": action,
            "reason": reason,
        })
        if len(self._moderation_logs) > 500:
            self._moderation_logs = self._moderation_logs[-400:]
            for i, log in enumerate(self._moderation_logs):
                log["id"] = i
        self._save_logs()

    # ==================== LLM 群管工具 ====================
    @filter.llm_tool(name="ban_group_member")
    async def ban_group_member_tool(self, event: AstrMessageEvent, user_id: str, duration_minutes: int = 10):
        '''禁言群成员。当用户要求禁言某人时使用此工具。

        Args:
            user_id(string): 要禁言的用户QQ号
            duration_minutes(number): 禁言时长（分钟），默认10分钟
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("ban_enabled", "禁言")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            duration_seconds = (min(max(duration_minutes, 1), 30 * 24 * 60) * 60)
            duration_seconds = (duration_seconds // 60) * 60
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            uid = self._safe_int(user_id, 0)
            if not gid or not uid:
                yield event.plain_result("群号或用户QQ号格式无效")
                return
            result = await client.call_action('set_group_ban', group_id=gid, user_id=uid, duration=duration_seconds)
            ok, err = self._check_api_result(result, "禁言")
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            yield event.plain_result(f"已禁言 {user_id} {duration_minutes}分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {e}")

    @filter.llm_tool(name="unban_group_member")
    async def unban_group_member_tool(self, event: AstrMessageEvent, user_id: str):
        '''解除群成员禁言。当用户要求解除某人禁言时使用此工具。

        Args:
            user_id(string): 要解除禁言的用户QQ号
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("unban_enabled", "解除禁言")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            uid = self._safe_int(user_id, 0)
            if not gid or not uid:
                yield event.plain_result("群号或用户QQ号格式无效")
                return
            result = await client.call_action('set_group_ban', group_id=gid, user_id=uid, duration=0)
            ok, err = self._check_api_result(result, "解除禁言")
            if not ok:
                yield event.plain_result(f"解除禁言失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解除禁言失败: {e}")

    @filter.llm_tool(name="kick_group_member")
    async def kick_group_member_tool(self, event: AstrMessageEvent, user_id: str):
        '''踢出群成员。当用户要求将某人踢出群时使用此工具。

        Args:
            user_id(string): 要踢出的用户QQ号
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("kick_enabled", "踢人")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            uid = self._safe_int(user_id, 0)
            if not gid or not uid:
                yield event.plain_result("群号或用户QQ号格式无效")
                return
            result = await client.call_action('set_group_kick', group_id=gid, user_id=uid, reject_add_request=False)
            ok, err = self._check_api_result(result, "踢人")
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 踢出群聊")
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

    @filter.llm_tool(name="set_whole_group_ban")
    async def set_whole_group_ban_tool(self, event: AstrMessageEvent, enable: bool = True):
        '''开启或关闭全体禁言。

        Args:
            enable(boolean): true开启全体禁言，false关闭全体禁言
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("whole_ban_enabled", "全体禁言")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_whole_ban', group_id=int(group_id), enable=enable)
            ok, err = self._check_api_result(result, "全体禁言")
            if not ok:
                yield event.plain_result(f"设置全体禁言失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"设置全体禁言失败: {e}")

    @filter.llm_tool(name="set_member_card")
    async def set_member_card_tool(self, event: AstrMessageEvent, user_id: str, card: str):
        '''设置群成员群名片。

        Args:
            user_id(string): 目标用户QQ号
            card(string): 新的群名片
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("set_card_enabled", "修改群名片")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            uid = self._safe_int(user_id, 0)
            if not gid or not uid:
                yield event.plain_result("群号或用户QQ号格式无效")
                return
            result = await client.call_action('set_group_card', group_id=gid, user_id=uid, card=card)
            ok, err = self._check_api_result(result, "设置群名片")
            if not ok:
                yield event.plain_result(f"设置群名片失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的群名片设为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置群名片失败: {e}")

    @filter.llm_tool(name="send_group_announcement")
    async def send_group_announcement_tool(self, event: AstrMessageEvent, content: str):
        '''发送群公告。

        Args:
            content(string): 公告内容
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("send_announcement_enabled", "发送群公告")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('_send_group_notice', group_id=gid, content=content)
            ok, err = self._check_api_result(result, "发送群公告")
            if not ok:
                yield event.plain_result(f"发布公告失败: {err}")
                return
            yield event.plain_result("群公告已发布")
        except Exception as e:
            yield event.plain_result(f"发布公告失败: {e}")

    @filter.llm_tool(name="get_group_member_list")
    async def get_group_member_list_tool(self, event: AstrMessageEvent):
        '''获取群成员列表。'''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("member_list_enabled", "查看群成员列表")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            if not members:
                yield event.plain_result("群成员列表为空")
                return
            member_texts = []
            for m in members[:30]:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                name = card if card else nickname
                role = m.get("role", "member")
                role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                member_texts.append(f"- {name}({m.get('user_id')}) [{role_text}]")
            yield event.plain_result(self._truncate(f"群成员（共{len(members)}人）：\n" + "\n".join(member_texts)))
        except Exception as e:
            yield event.plain_result(f"获取成员列表失败: {e}")

    @filter.llm_tool(name="set_group_admin")
    async def set_group_admin_tool(self, event: AstrMessageEvent, user_id: str, enable: bool = True):
        '''设置或取消群管理员。

        Args:
            user_id(string): 目标用户QQ号
            enable(boolean): true设为管理员，false取消管理员
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("set_admin_enabled", "设置管理员")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            uid = self._safe_int(user_id, 0)
            if not gid or not uid:
                yield event.plain_result("群号或用户QQ号格式无效")
                return
            result = await client.call_action('set_group_admin', group_id=gid, user_id=uid, enable=enable)
            ok, err = self._check_api_result(result, "设置管理员")
            if not ok:
                yield event.plain_result(f"设置管理员失败: {err}")
                return
            yield event.plain_result(f"已{'设为' if enable else '取消'} {user_id} 的管理员")
        except Exception as e:
            yield event.plain_result(f"设置管理员失败: {e}")

    @filter.llm_tool(name="set_group_name")
    async def set_group_name_tool(self, event: AstrMessageEvent, group_name: str):
        '''修改群名称。

        Args:
            group_name(string): 新的群名称
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("set_group_name_enabled", "修改群名称")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('set_group_name', group_id=gid, group_name=group_name)
            ok, err = self._check_api_result(result, "修改群名称")
            if not ok:
                yield event.plain_result(f"改群名失败: {err}")
                return
            yield event.plain_result(f"群名已改为: {group_name}")
        except Exception as e:
            yield event.plain_result(f"改群名失败: {e}")

    @filter.llm_tool(name="set_member_title")
    async def set_member_title_tool(self, event: AstrMessageEvent, user_id: str, title: str):
        '''设置群成员专属头衔。

        Args:
            user_id(string): 目标用户QQ号
            title(string): 专属头衔
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("set_title_enabled", "设置专属头衔")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            uid = self._safe_int(user_id, 0)
            if not gid or not uid:
                yield event.plain_result("群号或用户QQ号格式无效")
                return
            result = await client.call_action('set_group_special_title', group_id=gid, user_id=uid, special_title=title)
            ok, err = self._check_api_result(result, "设置头衔")
            if not ok:
                yield event.plain_result(f"设置头衔失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的头衔设为: {title}")
        except Exception as e:
            yield event.plain_result(f"设置头衔失败: {e}")

    @filter.llm_tool(name="get_banned_members")
    async def get_banned_members_tool(self, event: AstrMessageEvent):
        '''获取群禁言列表。'''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("banned_list_enabled", "查看禁言列表")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('get_group_shut_list', group_id=gid)
            shut_list = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            if not shut_list:
                yield event.plain_result("当前没有禁言成员")
                return
            member_texts = []
            for m in shut_list[:15]:
                uid = m.get("user_id", "")
                nickname = m.get("nickname", "")
                shut_time = self._safe_int(m.get("shut_up_timestamp", 0))
                if shut_time:
                    remain = max(0, shut_time - int(time.time()))
                    remain_str = f"{remain // 60}分{remain % 60}秒"
                else:
                    remain_str = "未知"
                member_texts.append(f"- {nickname}({uid}) 剩余: {remain_str}")
            yield event.plain_result(f"禁言列表（共{len(shut_list)}人）：\n" + "\n".join(member_texts))
        except Exception as e:
            yield event.plain_result(f"获取禁言列表失败: {e}")

    @filter.llm_tool(name="set_group_join_verify")
    async def set_group_join_verify_tool(self, event: AstrMessageEvent, verify_type: str = "allow"):
        '''设置群加群验证方式。

        Args:
            verify_type(string): 验证类型: allow(允许加入), deny(拒绝加入), need_verify(需要审核), not_allow(不允许)
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("join_verify_enabled", "设置加群方式")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            type_map = {"allow": 2, "deny": 1, "need_verify": 3, "not_allow": 4}
            add_type = type_map.get(verify_type.lower(), 2)
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('set_group_add_option', group_id=gid, add_type=add_type)
            ok, err = self._check_api_result(result, "设置加群方式")
            if not ok:
                yield event.plain_result(f"设置加群方式失败: {err}")
                return
            type_text = {"allow": "允许加入", "deny": "拒绝加入", "need_verify": "需审核", "not_allow": "不允许"}.get(verify_type.lower(), verify_type)
            yield event.plain_result(f"加群方式已设为: {type_text}")
        except Exception as e:
            yield event.plain_result(f"设置加群方式失败: {e}")

    @filter.llm_tool(name="recall_message")
    async def recall_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''撤回指定消息。

        Args:
            message_id(string): 要撤回的消息ID
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            result = await client.call_action('delete_msg', message_id=mid)
            ok, err = self._check_api_result(result, "撤回消息")
            if not ok:
                yield event.plain_result(f"撤回失败: {err}")
                return
            yield event.plain_result(f"已撤回消息 {message_id}")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    @filter.llm_tool(name="set_essence_message")
    async def set_essence_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''设置群精华消息。

        Args:
            message_id(string): 要设为精华的消息ID
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            result = await client.call_action('set_essence_msg', message_id=mid)
            ok, err = self._check_api_result(result, "设精华")
            if not ok:
                yield event.plain_result(f"设精华失败: {err}")
                return
            yield event.plain_result(f"已将 {message_id} 设为精华")
        except Exception as e:
            yield event.plain_result(f"设精华失败: {e}")

    @filter.llm_tool(name="delete_essence_message")
    async def delete_essence_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''取消群精华消息。

        Args:
            message_id(string): 要取消精华的消息ID
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            result = await client.call_action('delete_essence_msg', message_id=mid)
            ok, err = self._check_api_result(result, "取消精华")
            if not ok:
                yield event.plain_result(f"取消精华失败: {err}")
                return
            yield event.plain_result(f"已取消 {message_id} 的精华")
        except Exception as e:
            yield event.plain_result(f"取消精华失败: {e}")

    @filter.llm_tool(name="delete_group_notice")
    async def delete_group_notice_tool(self, event: AstrMessageEvent, notice_id: str):
        '''删除群公告。

        Args:
            notice_id(string): 公告ID
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("delete_announcement_enabled", "删除群公告")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('_del_group_notice', group_id=gid, notice_id=notice_id)
            ok, err = self._check_api_result(result, "删除公告")
            if not ok:
                yield event.plain_result(f"删除公告失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除公告失败: {e}")

    @filter.llm_tool(name="list_group_files")
    async def list_group_files_tool(self, event: AstrMessageEvent):
        '''查看群文件列表。'''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('get_group_root_files', group_id=gid)
            files = (result.get('files') or []) if isinstance(result, dict) else []
            folders = (result.get('folders') or []) if isinstance(result, dict) else []
            if not files and not folders:
                yield event.plain_result("根目录下没有文件或文件夹")
                return
            lines = [f"群 {group_id} 根目录："]
            if folders:
                lines.append(f"  {len(folders)}个文件夹")
                for f in folders[:10]:
                    lines.append(f"    [{f.get('folder_id', '')}] {f.get('folder_name', '')}")
            if files:
                lines.append(f"  {len(files)}个文件")
                for f in files[:10]:
                    size_mb = self._safe_int(f.get('file_size', 0)) / (1024 * 1024)
                    lines.append(f"    [{f.get('file_id', '')}] {f.get('file_name', '')} ({size_mb:.1f}MB)")
            yield event.plain_result(self._truncate("\n".join(lines)))
        except Exception as e:
            yield event.plain_result(f"查文件失败: {e}")

    @filter.llm_tool(name="delete_group_file")
    async def delete_group_file_tool(self, event: AstrMessageEvent, file_id: str, busid: int = 102):
        '''删除群文件。

        Args:
            file_id(string): 文件ID
            busid(number): 文件类型ID，默认为102
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('delete_group_file', group_id=gid, file_id=file_id, busid=busid)
            ok, err = self._check_api_result(result, "删除文件")
            if not ok:
                yield event.plain_result(f"删文件失败: {err}")
                return
            yield event.plain_result(f"已删除 {file_id}")
        except Exception as e:
            yield event.plain_result(f"删文件失败: {e}")

    @filter.llm_tool(name="get_group_notice_list")
    async def get_group_notice_list_tool(self, event: AstrMessageEvent):
        '''获取群公告列表。'''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("list_announcements_enabled", "查看公告列表")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            result = await client.call_action('_get_group_notice', group_id=gid)
            notices = (result.get('data') or []) if isinstance(result, dict) else result
            if not notices:
                yield event.plain_result("暂无公告")
                return
            lines = [f"群公告（{len(notices)}条）"]
            for n in notices[:10]:
                notice_id = n.get('notice_id', '')
                sender_id = n.get('sender_id', '')
                _msg = n.get('msg')
                content = ((_msg.get('text', '') if isinstance(_msg, dict) else '') or n.get('content', ''))[:60]
                ts = n.get('publish_time', 0)
                t = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '未知'
                lines.append(f"  [{notice_id}] {content}... ({sender_id}, {t})")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取公告失败: {e}")

    @filter.llm_tool(name="upload_group_file")
    async def upload_group_file_tool(self, event: AstrMessageEvent, file_path: str, file_name: str = ""):
        '''上传文件到群文件。

        Args:
            file_path(string): 文件路径
            file_name(string): 上传后的文件名，可选
        '''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        ok, msg = self._cfg_check("group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            if not file_path or not os.path.exists(file_path):
                yield event.plain_result(f"文件不存在: {file_path}")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            gid = self._safe_int(group_id, 0)
            if not gid:
                yield event.plain_result("群号格式无效")
                return
            name = file_name or os.path.basename(file_path)
            result = await client.call_action('upload_group_file', group_id=gid, file=file_path, name=name)
            fid = result.get('file_id', '未知') if isinstance(result, dict) else '未知'
            yield event.plain_result(f"已上传，file_id: {fid}")
        except Exception as e:
            yield event.plain_result(f"上传失败: {e}")

    # ==================== LLM 审核 ====================
    async def _fetch_context_messages(self, group_id: str, current_msg_id: str, count: int = 30) -> list:
        client = self._client or await self._get_client()
        if not client:
            return []
        try:
            gid = int(group_id)
        except (ValueError, TypeError):
            return []
        try:
            result = await client.call_action('get_group_msg_history',
                group_id=gid, message_seq=0, count=min(count + 5, 100))
            messages = result.get('messages', []) if isinstance(result, dict) else []
            return [m for m in messages if str(m.get('message_id', '')) != str(current_msg_id)][-count:]
        except Exception:
            return []

    def _extract_llm_text(self, response) -> str:
        if hasattr(response, 'completion_text'):
            return response.completion_text
        return str(response)

    async def _call_llm_safe(self, system_prompt: str, prompt: str) -> str:
        configured_id = str(self.config.get("moderation_llm_provider_id", "")).strip()
        errors = []

        async def _try_text_chat(prov, pid: str) -> str:
            if not hasattr(prov, 'text_chat'):
                return None
            signatures = [
                ((), {'system_prompt': system_prompt, 'prompt': prompt}),
                ((system_prompt + "\n\n" + prompt,), {}),
            ]
            for args, kwargs in signatures:
                try:
                    r = await prov.text_chat(*args, **kwargs)
                    if r:
                        return str(r)
                except (TypeError, ValueError):
                    continue
                except Exception as e:
                    err_str = str(e)[:120]
                    if not any(err_str in existing for existing in errors):
                        errors.append(f"{pid}.text_chat: {err_str}")
                    continue
            return None

        async def _try_provider(prov, pid: str) -> str:
            result = await _try_text_chat(prov, pid)
            if result:
                return result
            for meth in ('chat', 'invoke', 'complete'):
                fn = getattr(prov, meth, None)
                if not fn:
                    continue
                signatures = [
                    ((system_prompt + "\n\n" + prompt,), {}),
                    ((), {'prompt': system_prompt + "\n\n" + prompt}),
                ]
                for args, kwargs in signatures:
                    try:
                        r = await fn(*args, **kwargs)
                        if r:
                            return str(r)
                    except (TypeError, ValueError):
                        continue
                    except Exception as e:
                        err_str = str(e)[:120]
                        if not any(err_str in existing for existing in errors):
                            errors.append(f"{pid}.{meth}: {err_str}")
                        continue
            return None

        async def _try_by_id(pid: str) -> str:
            if hasattr(self.context, 'llm_generate'):
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=pid, prompt=prompt, system_prompt=system_prompt)
                    if resp:
                        return self._extract_llm_text(resp)
                except Exception as e:
                    err_str = str(e)[:120]
                    if not any(err_str in existing for existing in errors):
                        errors.append(f"llm_generate({pid}): {err_str}")
            prov = self.context.get_provider_by_id(pid) if hasattr(self.context, 'get_provider_by_id') else None
            if prov:
                result = await _try_provider(prov, pid)
                if result:
                    return result
            raise RuntimeError(f"Provider {pid} 不可用")

        if configured_id:
            try:
                result = await _try_by_id(configured_id)
                logger.info(f"[GroupMgr] LLM审核使用指定provider: {configured_id}")
                return result
            except Exception as e:
                err_str = str(e)[:120]
                if not any(err_str in existing for existing in errors):
                    errors.append(f"指定{configured_id}: {err_str}")

        try:
            ps = (self.context.get_all_providers() if hasattr(self.context, 'get_all_providers') else []) or []
        except Exception as e:
            ps = []
            err_str = str(e)[:120]
            if not any(err_str in existing for existing in errors):
                errors.append(f"get_all_providers: {err_str}")

        for p in ps:
            try:
                meta = p.meta()
                pid = meta.id
                result = await _try_by_id(pid)
                logger.info(f"[GroupMgr] LLM审核使用provider: {pid}")
                return result
            except Exception as e:
                err_str = str(e)[:80]
                if not any(err_str in existing for existing in errors):
                    errors.append(err_str)
                continue

        try:
            pm = getattr(self.context, 'provider_manager', None)
            if pm and hasattr(pm, 'get_using_provider'):
                up = pm.get_using_provider()
                if up:
                    result = await _try_provider(up, str(getattr(up, 'provider_name', up)))
                    if result:
                        logger.info("[GroupMgr] LLM审核使用provider_manager")
                        return result
        except Exception as e:
            err_str = str(e)[:120]
            if not any(err_str in existing for existing in errors):
                errors.append(f"provider_manager: {err_str}")

        detail = '; '.join(errors[:5]) if errors else '无任何可用Provider'
        raise RuntimeError(f"LLM调用失败({detail})。请检查AstrBot是否已配置LLM Provider")

    async def _call_llm_for_moderation(self, event: AiocqhttpMessageEvent,
                                        text: str, hit_types: Dict[str, bool]) -> dict:
        group_id = self._get_group_id(event)
        msg_obj = getattr(event, 'message_obj', None)
        msg_id = str(getattr(msg_obj, 'message_id', '')) if msg_obj else ''
        user_name = event.get_sender_name()
        context_msgs = []
        if group_id and msg_id:
            context_msgs = await self._fetch_context_messages(group_id, msg_id, 30)
        context_text = ""
        if context_msgs:
            lines = []
            for m in context_msgs:
                sender_obj = m.get('sender')
                sender = sender_obj.get('nickname', '未知') if isinstance(sender_obj, dict) else '未知'
                content = self._format_message_content(m.get('message', ''))
                lines.append(f"  {sender}: {content}")
            context_text = "\n".join(lines)
        suspect_types = [k for k, v in hit_types.items() if v]
        suspect_tag = "+".join(suspect_types) if suspect_types else "无"
        type_desc = {
            "swear": "骂人/脏话",
            "ad": "广告/推广",
            "political": "政治敏感",
            "porn": "色情/淫秽",
            "violent_terror": "暴恐内容",
            "reactionary": "反动言论",
            "weapons": "涉枪涉爆",
            "corruption": "贪腐相关",
            "illegal_url": "违规网址",
            "other": "其他违规",
            "supplement": "补充违规",
            "livelihood": "民生敏感",
            "tencent_ban": "腾讯封禁",
        }
        suspect_desc = "+".join([type_desc.get(t, t) for t in suspect_types]) if suspect_types else "无"
        prompt = (
            f"你是一名群聊内容审核员。你的任务是判断被标记消息是否需要撤回，需要结合上下文语境合理判断。\n\n"
            f"【核心准则】\n"
            f"- 侮辱性脏话（傻逼、废物、脑残、操你妈等）对任何对象使用都应撤回，包括对机器人\n"
            f"- 广告内容零容忍，一律撤回\n"
            f"- 政治敏感词库误报率高，需结合上下文判断，技术/游戏讨论不违规\n"
            f"- 色情/暴恐等需结合上下文判断\n"
            f"- 涉及查询、泄露他人隐私信息（身份证、住址、电话等）→ 违规\n\n"
            f"【审核标准】\n"
            f"1. 骂人/脏话类（swear）—— 严格处理侮辱性词汇：\n"
            f"     * 使用侮辱性脏话（傻逼、废物、蠢货、脑残、智障等）\n"
            f"     * 涉及家人死亡的诅咒（\"你妈死了\"、\"死全家\"、\"nmsl\"等）\n"
            f"     * 极端恶意人身攻击，明显带有仇恨和恶意\n"
            f"     * 对任何对象使用\"傻逼\"、\"操你妈\"、\"废物\"等侮辱性词汇\n"
            f"     * 对机器人/AI使用侮辱性脏话（\"傻逼机器人\"、\"废物机器人\"等）\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 轻微口头禅（\"卧槽\"、\"我靠\"、\"牛逼\"等不含侮辱性的语气词）\n"
            f"     * 自嘲、自黑（\"我太菜了\"、\"我真是个憨憨\"等）\n"
            f"     * 游戏中的轻度调侃（\"垃圾队友\"、\"这打得真烂\"等游戏场景）\n\n"
            f"2. 广告类（ad）—— 零容忍，一律违规：\n"
            f"   - 任何推广引流行为 → 违规（加微信、扫码、兼职、赚钱、收徒、挂圈等）\n"
            f"   - 色情引流（\"18+进xxx\"、\"看片加Q\"、\"福利群\"等）→ 违规\n"
            f"   - 金融诈骗（开户、跑分、洗钱、赌博等）→ 违规\n"
            f"   - 商品推销、代购、微商 → 违规\n"
            f"   - 任何包含联系方式（QQ号、微信号、手机号）的推广内容 → 违规\n"
            f"   - 只有纯粹的资源分享（如\"推荐一部电影\"）且无任何引流意图 → 不违规\n\n"
            f"3. 色情类（porn）—— 识别真正的色情内容：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 明确的色情内容、招嫖信息\n"
            f"     * 发送色情图片/视频/链接\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 暧昧玩笑、两性话题讨论（只要不过于露骨）\n"
            f"     * 恋爱话题、情感倾诉\n\n"
            f"4. 暴恐/涉枪涉爆/贪腐类：\n"
            f"   - 明确的违法内容 → 违规\n"
            f"   - 游戏/影视/新闻讨论 → 不违规\n\n"
            f"5. 政治敏感类（political）—— 注意：该词库误报率很高，需严格区分：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 明确的颠覆国家政权言论（\"推翻政府\"、\"颠覆政权\"等）\n"
            f"     * 直接侮辱国家领导人（不是讨论政策，而是人身攻击）\n"
            f"     * 明确煽动分裂国家的言论\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 正常政治讨论、新闻评论\n"
            f"     * 游戏、影视中的政治元素讨论\n"
            f"     * 历史人物/事件的正常讨论\n\n"
            f"6. 违规网址类（illegal_url）—— 注意：误报率高：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 赌博、色情、诈骗网站\n"
            f"     * 恶意软件下载链接\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 正常游戏攻略、教程链接\n"
            f"     * 视频网站链接（B站、YouTube等）\n"
            f"     * 工具软件官网\n\n"
            f"7. 隐私泄露类：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 泄露他人身份证号、住址、电话\n"
            f"     * 人肉搜索、开盒行为\n"
            f"     * 公开他人私人信息\n\n"
            f"请严格按照以下JSON格式返回，不要返回其他内容：\n"
            f'{{"violation": true/false, "reason": "判断原因"}}\n\n'
            f"【被标记消息】\n"
            f"发送者: {user_name}\n"
            f"内容: {text}\n"
            f"可疑类型: {suspect_desc} ({suspect_tag})\n\n"
            f"【上下文消息】\n"
            f"{context_text}\n"
        )
        system_prompt = (
            "你是一名群聊内容审核员。你的任务是判断被标记消息是否需要撤回。"
            "请结合上下文语境合理判断。返回严格的JSON格式。"
        )
        try:
            llm_response = await self._call_llm_safe(system_prompt, prompt)
            json_match = re.search(r'\{.*\}', llm_response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result
            else:
                logger.warning(f"[GroupMgr] LLM返回非JSON格式: {llm_response[:200]}")
                return {"violation": False, "reason": "LLM返回格式异常"}
        except json.JSONDecodeError as e:
            logger.warning(f"[GroupMgr] LLM返回JSON解析失败: {e}")
            return {"violation": False, "reason": "JSON解析失败"}
        except Exception as e:
            logger.warning(f"[GroupMgr] LLM审核调用失败: {e}")
            return {"violation": False, "reason": f"LLM调用失败: {str(e)[:100]}"}

    def _is_ad_pattern(self, text: str) -> bool:
        if not text:
            return False
        return any(p.search(text) for p in self._compiled_ad)

    def _should_scan_message(self, event: AiocqhttpMessageEvent) -> bool:
        if isinstance(event, AiocqhttpMessageEvent):
            sub_type = ''
            raw = getattr(event, 'raw_event', None)
            if isinstance(raw, dict):
                sub_type = str(raw.get('sub_type', '')).lower()
            if sub_type in ('anonymous', 'notice'):
                return False
            chain = event.get_messages()
            has_text = False
            for seg in (chain or []):
                if not isinstance(seg, dict):
                    has_text = True
                    break
                if seg.get('type') == 'text' and seg.get('data', {}).get('text', '').strip():
                    has_text = True
                    break
            return has_text
        return True

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def _handle_message(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            return
        if self.group_black_list and group_id in self.group_black_list:
            return
        if self.group_white_list and group_id not in self.group_white_list:
            return
        if not self._should_scan_message(event):
            return
        if not self._cfg("enabled"):
            return
        if await self._is_admin(event):
            return
        if self.user_black_list:
            user_id = self._try_get_sender_id(event)
            if user_id and user_id in self.user_black_list:
                try:
                    await self._kick_member(event)
                    await self._mute_member(event, 60)
                    notice = self.config.get("ban_notice", "[群管] {name}({uid}) 已被踢出（黑名单）")
                    yield event.plain_result(notice.replace("{name}", event.get_sender_name()).replace("{uid}", user_id).replace("{group}", group_id))
                    event.stop_event()
                except Exception as e:
                    logger.warning(f"[GroupMgr] 黑名单执行出错: {e}")
                return
        if not self.auto_moderate_enabled:
            return
        chain = event.get_messages()
        raw_text_parts = []
        for seg in (chain or []):
            if isinstance(seg, dict):
                if seg.get('type') == 'text':
                    raw_text_parts.append(seg.get('data', {}).get('text', ''))
            else:
                raw_text_parts.append(getattr(seg, 'text', '') or '')
        text = ''.join(raw_text_parts).strip()
        if not text:
            return
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        user_name = event.get_sender_name()

        hit_types = {
            "swear": False,
            "ad": False,
            "political": False,
            "porn": False,
            "violent_terror": False,
            "reactionary": False,
            "weapons": False,
            "corruption": False,
            "illegal_url": False,
            "other": False,
        }

        swear_hit = False
        if self._cfg("scan_swear", True):
            for p in self._compiled_swear:
                m = p.search(text)
                if m:
                    logger.info(f"[GroupMgr] 正则脏话命中: {m.group()} (pattern={p.pattern[:40]}...)")
                    swear_hit = True
                    break
        hit_types["swear"] = swear_hit

        ad_hit = False
        if self._cfg("scan_ad", True):
            ad_hit = self._is_ad_pattern(text)
        hit_types["ad"] = ad_hit

        lexicon_result = self._check_lexicon(text)
        for cat, hit in lexicon_result.items():
            if cat in hit_types:
                hit_types[cat] = hit

        should_check = any(hit_types.values())
        if not should_check:
            return

        if not self._cfg("llm_moderation_enabled", True):
            reason = "触发规则: " + ", ".join(k for k, v in hit_types.items() if v)
            logger.info(f"[GroupMgr] {user_name}({user_id}) in {group_id} -> {reason}")
            try:
                msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
                await self._recall_msg(event, msg_id)
                await self._mute_member(event)
                notice = self.config.get("ban_notice", "[群管] {name}({uid}) 已被禁言（触发规则）")
                yield event.plain_result(notice.replace("{name}", user_name).replace("{uid}", user_id).replace("{group}", group_id))
                self._log_moderation(group_id, user_id, user_name, text, "撤回+禁言", reason)
                event.stop_event()
            except Exception as e:
                logger.warning(f"[GroupMgr] 自动审核出错: {e}")
            return

        llm_result = await self._call_llm_for_moderation(event, text, hit_types)
        is_violation = llm_result.get("violation", False)
        reason = llm_result.get("reason", "无理由")

        if not is_violation:
            logger.info(f"[GroupMgr] LLM审核通过: {user_name}({user_id}) in {group_id} | 命中类型={{{', '.join(k for k, v in hit_types.items() if v)}}} | 原因={reason}")
            self._log_moderation(group_id, user_id, user_name, text, "LLM放行", reason)
            return

        logger.info(f"[GroupMgr] LLM审核拦截: {user_name}({user_id}) in {group_id} | 命中类型={{{', '.join(k for k, v in hit_types.items() if v)}}} | 原因={reason}")

        try:
            msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
            if msg_id:
                try:
                    await self._recall_msg(event, msg_id)
                except Exception as recall_err:
                    logger.warning(f"[GroupMgr] 撤回消息失败: {recall_err}")

            if self._cfg("llm_moderation_ban", True):
                try:
                    await self._mute_member(event)
                except Exception as ban_err:
                    logger.warning(f"[GroupMgr] 禁言失败: {ban_err}")

            if self._cfg("auto_moderate_notice", True):
                try:
                    notice = self.config.get("ban_notice", "[群管] {name}({uid}) 的消息已被撤回（违规内容）")
                    yield event.plain_result(notice.replace("{name}", user_name).replace("{uid}", user_id).replace("{group}", group_id))
                except Exception as notice_err:
                    logger.warning(f"[GroupMgr] 发送通知失败: {notice_err}")

            self._log_moderation(group_id, user_id, user_name, text, "LLM撤回", reason)
            event.stop_event()
        except Exception as e:
            logger.warning(f"[GroupMgr] 自动审核出错: {e}")
            yield event.plain_result(f"[群管] 审核出错: {str(e)[:100]}")

    async def _recall_msg(self, event: AiocqhttpMessageEvent, msg_id: str):
        mid = self._safe_int(msg_id)
        if not mid:
            return
        client = await self._get_client(event)
        if not client:
            return
        try:
            await client.call_action('delete_msg', message_id=mid)
        except Exception as e:
            logger.warning(f"[GroupMgr] 撤回消息失败: {e}")

    async def _kick_member(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return
        client = await self._get_client(event)
        if not client:
            return
        try:
            await client.call_action('set_group_kick', group_id=int(group_id), user_id=int(user_id))
        except Exception as e:
            logger.warning(f"[GroupMgr] 踢人失败: {e}")

    async def _mute_member(self, event: AiocqhttpMessageEvent, duration: int = None):
        group_id = self._get_group_id(event)
        user_id = self._try_get_sender_id(event)
        if not group_id or not user_id:
            return
        client = await self._get_client(event)
        if not client:
            return
        ban_duration = duration if duration is not None else int(self.config.get("moderation_ban_duration", 1800))
        try:
            await client.call_action('set_group_ban', group_id=int(group_id), user_id=int(user_id), duration=ban_duration)
        except Exception as e:
            logger.warning(f"[GroupMgr] 禁言失败: {e}")

    @filter.command("字数统计")
    async def word_count(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("word_count_enabled", "字数统计")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /字数统计 <关键词> [天数] [类型]\n类型: 脏话/广告/敏感词/黑名单\n示例: /字数统计 傻逼 7 脏话")
            return
        keyword = args[1]
        days = 7
        search_type = "all"
        type_map = {"脏话": "swear", "广告": "ad", "敏感词": "sensitive", "黑名单": "black"}
        if len(args) >= 3:
            try:
                days = int(args[2])
            except ValueError:
                search_type = type_map.get(args[2], args[2].lower())
        if len(args) >= 4:
            search_type = type_map.get(args[3], args[3].lower())
        days = max(1, min(days, 90))
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        try:
            count, sample_messages = await self._search_keyword_in_messages(event, group_id, keyword, days, search_type)
            if count == 0:
                yield event.plain_result(f"最近 {days} 天内未找到包含「{keyword}」的消息")
            else:
                result = f"最近 {days} 天内「{keyword}」出现次数: {count}\n"
                if sample_messages:
                    result += "\n最近消息:\n"
                    for msg in sample_messages[:5]:
                        result += f"  {msg}\n"
                yield event.plain_result(result)
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    async def _search_keyword_in_messages(self, event: AstrMessageEvent, group_id: str, keyword: str, days: int, search_type: str = "all") -> Tuple[int, list]:
        client = await self._get_client(event)
        if not client:
            return 0, []
        try:
            result = await client.call_action('get_group_msg_history', group_id=int(group_id), count=100)
            messages = result.get('messages', []) if isinstance(result, dict) else []
        except Exception as e:
            logger.warning(f"[GroupMgr] 获取历史消息失败: {e}")
            return 0, []
        now = int(time.time())
        cutoff = now - days * 24 * 3600
        count = 0
        sample_messages = []
        for msg in messages:
            try:
                msg_time = msg.get('time', 0)
                if msg_time < cutoff:
                    continue
                raw_message = msg.get('message', '')
                text = self._format_message_content(raw_message)
                if keyword.lower() in text.lower():
                    if search_type != "all":
                        is_match = False
                        if search_type == "swear":
                            is_match = any(p.search(text) for p in self._compiled_swear)
                        elif search_type == "ad":
                            is_match = self._is_ad_pattern(text)
                        elif search_type == "sensitive":
                            is_match = any(p.search(text) for p in self._compiled_lexicon.get("political", []))
                        elif search_type == "black":
                            sender = msg.get('sender', {})
                            uid = str(sender.get('user_id', ''))
                            is_match = uid in self.user_black_list
                        if not is_match:
                            continue
                    count += 1
                    sender = msg.get('sender', {})
                    nickname = sender.get('nickname', '未知')
                    sample_messages.append(f"{nickname}: {text[:50]}")
            except Exception:
                continue
        return count, sample_messages

    @filter.command("群统计")
    async def group_stats(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("group_stats_enabled", "群统计")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_member_list', group_id=int(group_id))
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            total = len(members)
            admins = sum(1 for m in members if m.get('role') in ('admin', 'owner'))
            owners = sum(1 for m in members if m.get('role') == 'owner')
            regular = total - admins
            stats = (
                f"群 {group_id} 统计:\n"
                f"  群主: {owners}人\n"
                f"  管理员: {admins - owners}人\n"
                f"  普通成员: {regular}人\n"
                f"  总计: {total}人"
            )
            yield event.plain_result(stats)
        except Exception as e:
            yield event.plain_result(f"获取统计失败: {e}")

    @filter.command("搜索成员")
    async def search_member(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("member_list_enabled", "查看群成员")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /搜索成员 <关键词>")
            return
        keyword = args[1]
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_member_list', group_id=int(group_id))
            members = result if isinstance(result, list) else (result.get("data") or []) if isinstance(result, dict) else []
            matched = []
            for m in members:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                uid = str(m.get("user_id", ""))
                if keyword.lower() in card.lower() or keyword.lower() in nickname.lower() or keyword in uid:
                    matched.append(m)
            if not matched:
                yield event.plain_result(f"未找到匹配「{keyword}」的成员")
            else:
                result_text = f"找到 {len(matched)} 个匹配成员:\n"
                for m in matched[:20]:
                    card = m.get("card", "")
                    nickname = m.get("nickname", "")
                    name = card if card else nickname
                    role = m.get("role", "member")
                    role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                    result_text += f"  {name}({m.get('user_id')}) [{role_text}]\n"
                yield event.plain_result(result_text.strip())
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回最新消息")
    async def recall_last(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        args = event.message_str.split()
        count = 1
        if len(args) >= 2:
            try:
                count = int(args[1])
            except ValueError:
                pass
        count = max(1, min(count, 10))
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_msg_history', group_id=int(group_id), count=count + 1)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            for msg in messages[-count:]:
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                    except Exception:
                        pass
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁言")
    async def cmd_ban(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("ban_enabled", "禁言")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /禁言 <QQ号> [时长(分钟)]\n示例: /禁言 123456 30")
            return
        try:
            user_id = str(args[1]).strip()
            duration = min(max(int(args[2]) if len(args) > 2 else 10, 1), 43200)
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_ban', group_id=int(group_id), user_id=int(user_id), duration=duration * 60)
            ok, err = self._check_api_result(result, "禁言")
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            yield event.plain_result(f"已禁言 {user_id}，时长 {duration} 分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解禁")
    async def cmd_unban(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("unban_enabled", "解禁")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /解禁 <QQ号>\n示例: /解禁 123456")
            return
        try:
            user_id = str(args[1]).strip()
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_ban', group_id=int(group_id), user_id=int(user_id), duration=0)
            ok, err = self._check_api_result(result, "解禁")
            if not ok:
                yield event.plain_result(f"解禁失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解禁失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("踢人")
    async def cmd_kick(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("kick_enabled", "踢人")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /踢人 <QQ号>\n示例: /踢人 123456")
            return
        try:
            user_id = str(args[1]).strip()
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_kick', group_id=int(group_id), user_id=int(user_id))
            ok, err = self._check_api_result(result, "踢人")
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 踢出群聊")
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("全体禁言")
    async def cmd_whole_ban(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("whole_ban_enabled", "全体禁言")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        enable = True
        if len(args) >= 2:
            action = args[1].strip()
            if action in ("关闭", "off", "0", "取消"):
                enable = False
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_whole_ban', group_id=int(group_id), enable=enable)
            ok, err = self._check_api_result(result, "全体禁言")
            if not ok:
                yield event.plain_result(f"操作失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置名片")
    async def cmd_set_card(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("set_card_enabled", "设置名片")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("用法: /设置名片 <QQ号> <名片内容>\n示例: /设置名片 123456 管理员")
            return
        try:
            user_id = str(args[1]).strip()
            card = ' '.join(args[2:])
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_card', group_id=int(group_id), user_id=int(user_id), card=card)
            ok, err = self._check_api_result(result, "设置名片")
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的群名片为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发公告")
    async def cmd_send_notice(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("send_announcement_enabled", "发公告")
        if not ok:
            yield event.plain_result(msg)
            return
        content = event.message_str.replace("/发公告", "").strip()
        if not content:
            yield event.plain_result("用法: /发公告 <公告内容>")
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            r = await client.call_action('_send_group_notice', group_id=int(group_id), content=content)
            api_ok, err = self._check_api_result(r, "发公告")
            if not api_ok:
                yield event.plain_result(f"发送失败: {err}")
                return
            notice_id = (r or {}).get("notice_id") or (r or {}).get("id") or ""
            yield event.plain_result(f"公告已发送{f'，ID: {notice_id}' if notice_id else ''}")
        except Exception as e:
            yield event.plain_result(f"发送失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删公告")
    async def cmd_delete_notice(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("delete_announcement_enabled", "删公告")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删公告 <公告ID>")
            return
        try:
            notice_id = str(args[1]).strip()
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('_del_group_notice', group_id=int(group_id), notice_id=notice_id)
            api_ok, err = self._check_api_result(result, "删公告")
            if not api_ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    @filter.command("公告列表")
    async def cmd_list_notices(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("list_announcements_enabled", "公告列表")
        if not ok:
            yield event.plain_result(msg)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('_get_group_notice', group_id=int(group_id))
            notices = result.get("notices", []) if isinstance(result, dict) else []
            if not notices:
                yield event.plain_result("暂无群公告")
                return
            lines = [f"📋 群公告列表 ({len(notices)}条):"]
            for n in notices[:10]:
                nid = n.get("notice_id", n.get("id", ""))
                pub = n.get("publisher", {})
                name = pub.get("nickname", "未知")
                title = n.get("title", n.get("content", ""))[:40]
                lines.append(f"  ID:{nid} | {name}: {title}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.command("文件列表")
    async def cmd_list_files(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("group_files_enabled", "群文件管理")
        if not ok:
            yield event.plain_result(msg)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('get_group_root_files', group_id=int(group_id))
            files = result.get("files", []) if isinstance(result, dict) else []
            folders = result.get("folders", []) if isinstance(result, dict) else []
            lines = [f"📁 群文件列表:"]
            for f in folders[:15]:
                lines.append(f"  📁 {f.get('folder_name', '?')}")
            for f in files[:15]:
                size = f.get('size', 0)
                unit = "B"
                if size > 1024 * 1024:
                    size, unit = round(size / 1048576, 1), "MB"
                elif size > 1024:
                    size, unit = round(size / 1024, 1), "KB"
                lines.append(f"  📄 {f.get('file_name', '?')} ({size}{unit})")
            if not files and not folders:
                lines.append("  暂无文件")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删文件")
    async def cmd_delete_file(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("group_files_enabled", "群文件管理")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删文件 <file_id>\n提示: 使用 /文件列表 查看 file_id")
            return
        try:
            file_id = str(args[1]).strip()
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('delete_group_file', group_id=int(group_id), file_id=file_id, busid=0)
            api_ok, err = self._check_api_result(result, "删文件")
            if not api_ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除文件 {file_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    @filter.command("成员列表")
    async def cmd_member_list(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("member_list_enabled", "成员列表")
        if not ok:
            yield event.plain_result(msg)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('get_group_member_list', group_id=int(group_id))
            members = result if isinstance(result, list) else []
            role_count = {"owner": 0, "admin": 0, "member": 0}
            for m in members:
                role = m.get("role", "member")
                role_count[role] = role_count.get(role, 0) + 1
            total = len(members)
            lines = [
                f"👥 群成员列表 ({total}人):",
                f"  👑 群主: {role_count['owner']}人",
                f"  ⭐ 管理员: {role_count['admin']}人",
                f"  👤 成员: {role_count['member']}人",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.command("禁言列表")
    async def cmd_banned_list(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("banned_list_enabled", "禁言列表")
        if not ok:
            yield event.plain_result(msg)
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('get_group_shut_list', group_id=int(group_id))
            banned = result if isinstance(result, list) else []
            if not banned:
                yield event.plain_result("当前无人被禁言")
                return
            lines = [f"🚫 禁言列表 ({len(banned)}人):"]
            for b in banned[:20]:
                uid = b.get("user_id", "?")
                dur = b.get("duration", 0)
                lines.append(f"  QQ: {uid}, 剩余: {dur // 60}分钟")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("群名")
    async def cmd_set_name(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("set_group_name_enabled", "修改群名")
        if not ok:
            yield event.plain_result(msg)
            return
        name = event.message_str.replace("/群名", "").strip()
        if not name:
            yield event.plain_result("用法: /群名 <新群名>")
            return
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_name', group_id=int(group_id), group_name=name)
            api_ok, err = self._check_api_result(result, "修改群名")
            if not api_ok:
                yield event.plain_result(f"修改失败: {err}")
                return
            yield event.plain_result(f"群名已修改为: {name}")
        except Exception as e:
            yield event.plain_result(f"修改失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("头衔")
    async def cmd_set_title(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("set_title_enabled", "设置头衔")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("用法: /头衔 <QQ号> <头衔内容>\n示例: /头衔 123456 大佬")
            return
        try:
            user_id = str(args[1]).strip()
            title = ' '.join(args[2:])
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_special_title', group_id=int(group_id), user_id=int(user_id), special_title=title, duration=-1)
            api_ok, err = self._check_api_result(result, "设置头衔")
            if not api_ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的专属头衔: {title}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设精华")
    async def cmd_set_essence(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /设精华 <message_id>\n回复消息或提供 message_id")
            return
        try:
            msg_id = int(args[1])
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_essence_msg', message_id=msg_id)
            api_ok, err = self._check_api_result(result, "设精华")
            if not api_ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设为精华消息 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消精华")
    async def cmd_del_essence(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /取消精华 <message_id>")
            return
        try:
            msg_id = int(args[1])
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('delete_essence_msg', message_id=msg_id)
            api_ok, err = self._check_api_result(result, "取消精华")
            if not api_ok:
                yield event.plain_result(f"取消失败: {err}")
                return
            yield event.plain_result(f"已取消精华 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"取消失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置管理")
    async def cmd_set_admin(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("set_admin_enabled", "设置管理员")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /设置管理 <QQ号>\n示例: /设置管理 123456")
            return
        try:
            user_id = str(args[1]).strip()
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_admin', group_id=int(group_id), user_id=int(user_id), enable=True)
            api_ok, err = self._check_api_result(result, "设置管理员")
            if not api_ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 设为群管理员")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("加群方式")
    async def cmd_join_verify(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("join_verify_enabled", "加群验证")
        if not ok:
            yield event.plain_result(msg)
            return
        args = event.message_str.split()
        method_map = {"需要验证": 1, "允许": 0, "禁止": 2, "免审核": 0}
        if len(args) < 2:
            yield event.plain_result("用法: /加群方式 <方法>\n方法: 需要验证/允许/禁止\n示例: /加群方式 需要验证")
            return
        try:
            method_str = args[1].strip()
            method = method_map.get(method_str, -1)
            if method == -1:
                yield event.plain_result("无效的方法，请选择: 需要验证/允许/禁止")
                return
            group_id = self._get_group_id(event)
            if not group_id:
                yield event.plain_result("无法获取群号")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            result = await client.call_action('set_group_add_option', group_id=int(group_id), add_type=method)
            api_ok, err = self._check_api_result(result, "加群方式")
            if not api_ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"加群方式已设置为: {method_str}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("自动审核")
    async def cmd_auto_moderate(self, event: AstrMessageEvent):
        args = event.message_str.split()
        if len(args) < 2:
            status = "开启" if self.auto_moderate_enabled else "关闭"
            yield event.plain_result(f"自动审核状态: {status}\n用法: /自动审核 开启|关闭")
            return
        action = args[1].strip()
        if action in ("开启", "on", "1"):
            self.auto_moderate_enabled = True
            self.config["auto_moderate_enabled"] = True
        elif action in ("关闭", "off", "0"):
            self.auto_moderate_enabled = False
            self.config["auto_moderate_enabled"] = False
        else:
            yield event.plain_result("参数错误，请使用: 开启 或 关闭")
            return
        self._save_config_safe()
        yield event.plain_result(f"自动审核已{action}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置管理插件")
    async def cmd_plugin_admin(self, event: AstrMessageEvent):
        args = event.message_str.split()
        if len(args) < 2:
            admins = self.config.get("admin_list", [])
            yield event.plain_result(f"插件管理员 ({len(admins)}人): {', '.join(str(a) for a in admins) or '无'}\n用法: /设置管理插件 <QQ号> 添加/移除")
            return
        user_id = str(args[1]).strip()
        action = "添加" if len(args) < 3 else args[2].strip()
        admin_list = self.config.get("admin_list", [])
        if not isinstance(admin_list, list):
            admin_list = []
        admin_list = [str(a).strip() for a in admin_list if a]
        if action == "移除":
            if user_id in admin_list:
                admin_list.remove(user_id)
                yield event.plain_result(f"已移除插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 不在管理员列表中")
        else:
            if user_id not in admin_list:
                admin_list.append(user_id)
                yield event.plain_result(f"已添加插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 已是插件管理员")
        self.config["admin_list"] = admin_list
        self._save_config_safe()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批量撤回")
    async def recall_all(self, event: AstrMessageEvent):
        ok, msg = self._cfg_check("recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        args = event.message_str.split()
        user_id = None
        if len(args) >= 2:
            user_id = args[1]
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_msg_history', group_id=int(group_id), count=100)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            for msg in messages:
                sender = msg.get('sender', {})
                uid = str(sender.get('user_id', ''))
                if user_id and uid != user_id:
                    continue
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                    except Exception:
                        pass
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")