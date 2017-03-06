#!/usr/bin/env python
# started from
# https://github.com/razius/flask-github-webhook/blob/master/index.py
import io
import os
import re
import sys
import json
import re
import subprocess
import requests
import ipaddress
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.generator import Generator
import email.charset
from cStringIO import StringIO

email.charset.add_charset('utf-8', email.charset.QP, email.charset.QP, 'utf-8')

class InvalidConfiguration(Exception):
    pass

def validate_repos(config):
    # TODO: Check that all configured repos have events with matching templates?
    # that they all have an email.to field?
    mls = json.loads(io.open(config['mls'], 'r').read())
    import os.path
    for (ml, repos) in mls.iteritems():
        for (repo,data) in repos.iteritems():
            for e in data.get("events",[]):
                generic_template = config['TEMPLATES_DIR'] + '/generic/' + e
                ml_template = config['TEMPLATES_DIR'] + '/mls/' + ml + '/' + e
                specific_template = config['TEMPLATES_DIR'] + '/mls/' + ml + '/' + repo + '/' + e
                if not (os.path.isfile(generic_template) or os.path.isfile(ml_template)
                        or os.path.isfile(specific_template)):
                    raise InvalidConfiguration("No template matching event %s defined in %s in %s (looked at %s and %s)" % (e, config['mls'], repo, generic_template, specific_template))

def event_id(event, payload):
    if event.split(".")[0] == "issues":
        return payload["issue"]["id"]
    elif event.split(".")[0] == "issue_comment":
        return payload["comment"]["id"]
    elif event == "push":
        return payload["head_commit"]["id"]
    elif event.split(".")[0] == "pull_request":
        return payload["pull_request"]["id"]

def event_timestamp(event, payload):
    def timestamp(date):
        from dateutil import parser
        import calendar
        try:
            return calendar.timegm(parser.parse(date).utctimetuple())
        except:
            return date
    ts = None
    if event == "push":
        ts = payload["repository"]["pushed_at"]
    elif event == "issue_comment.created":
        ts = payload["comment"]["created_at"]
    elif event.split(".")[0] in ["issues", "pull_request"]:
        action = event.split(".")[1]
        key = "pull_request" if event.split(".")[0] == "pull_request" and payload.has_key("pull_request") else "issue"
        if action == "opened":
            ts = payload[key]["created_at"]
        elif action == "closed":
            ts = payload[key]["closed_at"]
        elif action == "reopened" or action == "synchronize":
            ts = payload[key]["updated_at"]
    if ts:
        return timestamp(ts)

def refevent(event, payload, target, oauth_token):
    if target=="issue" and event in ["issues.reopened", "issues.closed", "issue_comment.created"]:
        return ("issues.opened", payload["issue"]["id"])
    elif target=="pull_request" and event in ["pull_request.closed", "pull_request.reopened",
                   "pull_request.synchronized",
                                              "pull_request_review_comment.created"]:
        return ("pull_request.opened", payload["pull_request"]["id"])
    elif target == "pull_request" and event == "issue_comment.created":
        if oauth_token:
            headers = {}
            headers['Authorization']="token %s" % (oauth_token)
            pr_id = requests.get(payload['issue']['pull_request']['url'],
                                     headers=headers).json()['id']
            if pr_id:
                return ("pull_request.opened", pr_id)
    return (None,None)

def navigateGithubList(url, token, until, cumul = []):
    headers = {}
    headers['Authorization']="token %s" % token
    githubListReq = requests.get(url, headers=headers)
    pageList = githubListReq.json()
    def posterior(item):
        return until.strftime("%Y-%m-%dT%H:%M:%SZ") <= item["created_at"]
    cumul = cumul + filter(posterior, pageList)
    if len(pageList) and posterior(pageList[-1]) and githubListReq.links.get("next", {}).has_key("url"):
        return navigateGithubList(githubListReq.links["next"]["url"], token, until, cumul)
    else:
        return cumul

def listGithubEvents(repo, token, until):
    baseUrl = "https://api.github.com/repos/%s/" % repo
    events = {}
    events["repo"] = navigateGithubList(baseUrl + "events", token, until)
    #events["issues"] = navigateGithubList(baseUrl + "issues/events", token, until)
    return events

def andify(l):
    return [{"name":x, "last": i ==len(l) -1} for i,x in enumerate(l)]


def extractDigestInfo(events, eventFilter=None):
    def listify(l):
        return {"count": len(l), "list":l }

    data = {}
    isIssue = lambda x: x.get("type") == "IssuesEvent"
    isPR = lambda x: x.get("type") == "PullRequestEvent"
    isComment = lambda x: x.get("type") == "IssueCommentEvent"
    isNew = lambda x: x.get("payload",{}).get("action") == "opened"
    isCreated = lambda x: x.get("payload",{}).get("action") == "created"
    isClosed = lambda x: x.get("payload",{}).get("action") == "closed"
    isMerged = lambda x: x.get("payload",{}).get("pull_request",{}).get("merged")

    filtered_events = events["repo"]
    if (eventFilter):
        filtered_events = filter(lambda x: filter_event_payload(eventFilter, x.get("payload", {})), events["repo"])

    newissues = filter(isNew, filter(isIssue, filtered_events))
    closedissues = filter(isClosed, filter(isIssue, filtered_events))
    newpr = filter(isNew, filter(isPR, filtered_events))
    mergedpr = filter(isMerged, filter(isClosed, filter(isPR, filtered_events)))

    issuecomments = filter(isCreated, filter(isComment, filtered_events))
    commentedissues = {}
    for comment in issuecomments:
        number = comment["payload"]["issue"]["number"]
        if not commentedissues.has_key(number):
            issue = {}
            issue["number"] = number
            issue["title"] = comment["payload"]["issue"]["title"]
            issue["url"] = comment["payload"]["issue"]["html_url"]
            issue["commentscount"] = 0
            issue["commentors"] = set()
            issue["ispr"] = comment["payload"]["issue"].has_key("pull_request")
            commentedissues[number] = issue
        commentedissues[number]["commentscount"] += 1
        commentedissues[number]["commentors"].add(comment["actor"]["display_login"])
    for number, issue in commentedissues.iteritems():
        commentedissues[number]["commentors"] = andify(commentedissues[number]["commentors"])
    data["newissues"] = listify(newissues)
    data["closedissues"] = listify(closedissues)
    data["commentedissues"] = listify(sorted(filter(lambda x: not x["ispr"], commentedissues.values()), key=lambda issue: -issue["commentscount"]))
    data["issuecommentscount"] = reduce(lambda a,b: a + b["commentscount"], data["commentedissues"]["list"], 0)
    data["newpr"] = listify(newpr)
    data["mergedpr"] = listify(mergedpr)
    data["commentedpr"] = listify(sorted(filter(lambda x: x["ispr"], commentedissues.values()), key=lambda issue: -issue["commentscount"]))
    data["prcommentscount"] = reduce(lambda a,b: a + b["commentscount"], data["commentedpr"]["list"], 0)
    data["activeissue"] = len(newissues) > 0 or len(closedissues) >0 or data["issuecommentscount"] > 0
    data["activepr"] = data["prcommentscount"] > 0 or len(newpr) > 0 or len(mergedpr) > 0
    return data

def sendDigest(config, period="daily"):
    from datetime import datetime, timedelta
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if period in days:
        until = datetime.now() - timedelta(7)
        duration = "weekly"
    else:
        until = datetime.now() - timedelta(1)
        duration = period
    mls = json.loads(io.open(config['mls'], 'r').read())
    token = config.get("GH_OAUTH_TOKEN", False)
    digests = {}
    for (ml, target) in mls.iteritems():
        if target.has_key("digest:%s" % period.lower()):
            digests[ml] = target["digest:%s" % period.lower()]
            if not isinstance(digests[ml], list):
                digests[ml] = [digests[ml]]
    for (ml, digest) in digests.iteritems():
        for d in digest:
            repos = d["repos"]
            events = {}
            events["repos"] = [{"name": r, "shortname": r.split("/")[1], "url": "https://github.com/" + r, "last": i==len(repos)-1} for i,r in enumerate(repos)]
            events["activeissuerepos"] = []
            events["activeprrepos"] = []
            events["period"] = duration.capitalize()
            for repo in repos:
                data = extractDigestInfo(listGithubEvents(repo, token, until), d.get("eventFilter", None))
                data["name"] = repo
                if data["activeissue"]:
                    events["activeissuerepos"].append(data)
                if data["activepr"]:
                    events["activeprrepos"].append(data)
                events["filtered"] = d.get("eventFilter", None)
                events["labels"] = andify(d.get("eventFilter", {}).get("label", []))
                events["topic"] = d.get("topic", None)
            events["activeissues"] = len(events["activeissuerepos"])
            events["activeprs"] = len(events["activeprrepos"])
            if events["activeissues"] > 0 or events["activeprs"] > 0:
                template, error = loadTemplate("digest", config["TEMPLATES_DIR"], '/mls/' + ml + '/', duration)
                if not template:
                    raise InvalidConfiguration("No template for %s digest targeted at %s" % (duration, ml))
                from_addr = config.get("email", {}).get("from", config["EMAIL_FROM"])
                body, subject = mailFromTemplate(template, events)
                to = ml.split(",")
                sendMail(config["SMTP_HOST"], body, from_addr, "W3C Webmaster via GitHub API", to, subject)

def serveRequest(config, postbody):
    request_method = os.environ.get('REQUEST_METHOD', "GET")
    if request_method == 'GET':
        output = "Content-Type: text/plain; charset=utf-8\n\n"
        output += " Nothing to see here, move along ..."
        return output
    if request_method != 'POST':
        return
    if os.environ.has_key('HTTP_X_GITHUB_EVENT'):
        return githubRequest(config, postbody)
    elif os.environ.has_key('HTTP_X_W3C_WEBHOOK'):
        return w3cRequest(config, postbody)

def w3cRequest(config, postbody):
    mls = json.loads(io.open(config['mls'], 'r').read())

    payload = json.loads(postbody)
    event = payload["event"]

    def trimTrailingSlash(s):
        import re
        return re.sub(r'/$', '', s)

    trs = {}
    tr_prefix = "http://www.w3.org/TR/"
    for (ml, mltr) in mls.iteritems():
        for (url, conf) in mltr.iteritems():
            if (url[0:len(tr_prefix)] == tr_prefix):
                url = trimTrailingSlash(url)
                conf["email"] = {"to": ml}
                if (event in conf["events"]):
                    if not trs.has_key("url"):
                        trs[url] = []
                    trs[url].append(conf)
    target = trimTrailingSlash(payload["specversion"]["shortlink"])
    sentMail = []
    errors = []
    for conf in trs.get(target, []):
        to = conf["email"].get("to").split(",")
        template, error = loadTemplate(event, config["TEMPLATES_DIR"], '/mls/' + ml + '/')
        if not template:
            errors.append(error)
            continue
        from_addr = conf.get("email", {}).get("from", config["EMAIL_FROM"])
        body, subject = mailFromTemplate(template, payload["specversion"])
        sentMail.append(sendMail(config["SMTP_HOST"], body, from_addr, "W3C Webmaster via W3C API", to, subject))
    return reportSentMail(sentMail, errors)

def filter_event_payload(eventFilter, payload):
    labels = eventFilter["label"]
    # backwards compat, since initially this took a single string
    # see https://github.com/dontcallmedom/github-notify-ml/issues/22
    if labels and not type(labels) == list:
        labels = [labels]
    labelTarget = payload.get("issue", payload.get("pull_request", {})).get("labels", [])
    labelFilter = lambda x: x.get("name") in labels
    if labels:
        if labelFilter(payload.get("label", {})):
            return payload
        return filter(labelFilter, labelTarget)
    return payload


def githubRequest(config, postbody):
    remote_addr = os.environ.get('HTTP_X_FORWARDED_FOR', os.environ.get('REMOTE_ADDR'))

    # Store the IP address blocks that github uses for hook requests.
    hook_blocks = requests.get('https://api.github.com/meta').json()['hooks']
    output = ""

    # Check if the request is from github.com
    for block in hook_blocks:
        ip = ipaddress.ip_address(u'%s' % remote_addr)
        if ipaddress.ip_address(ip) in ipaddress.ip_network(block):
            break #the remote_addr is within the network range of github
    else:
        output += "Status: 403 Unrecognized IP\n"
        output += "Content-Type: application/json\n\n"
        output += json.dumps({'msg': 'Unrecognized IP address', 'ip': remote_addr})
        return output

    event = os.environ.get('HTTP_X_GITHUB_EVENT', None)
    if event == "ping":
        output += "Content-Type: application/json\n\n"
        output += json.dumps({'msg': 'Hi!'})
        return output
    mls = json.loads(io.open(config['mls'], 'r').read())
    for (ml, mlrepos) in mls.iteritems():
        for (reponame, repoconf) in mlrepos.iteritems():
            # don't fail on digests which takes a list rather than a dict
            if type(repoconf) != list:
                repoconf["email"] = {"to":ml}
    payload = json.loads(postbody)
    repo_meta = {
	    'name': payload['repository'].get('name')
	    }
    repo_meta['owner'] = payload['repository']['owner'].get('name', payload['repository']['owner'].get('login'))
    match = re.match(r"refs/heads/(?P<branch>.*)", payload.get('ref', ''))
    if match:
        repo_meta['branch'] = match.groupdict()['branch']

    formatedRepoName = "{owner}/{name}".format(**repo_meta)

    def repoMatch(reponame):
        if (reponame.startswith("regexp:")):
            regexp = reponame[len("regexp:"):]
            try:
                return re.match(regexp, formatedRepoName) != None
            except:
                return False
        else:
            return reponame == formatedRepoName

    sentMail = []
    errors = []

    if payload.has_key("action"):
        event = event + "." + payload['action']

    for ml,repos in mls.iteritems():
        for reponame in filter(repoMatch, repos.keys()):
            tr_prefix = "http://www.w3.org/TR/"
            digest_prefix = "digest:"
            if reponame[0:len(tr_prefix)] == tr_prefix:
                continue
            if reponame[0:len(digest_prefix)] == digest_prefix:
                continue
            repo = repos[reponame]

            if event not in repo['events'] and (not repo_meta.has_key("branch") or event not in repo.get('branches', {}).get(repo_meta['branch'], [])):
                continue
            if repo.has_key("eventFilter"):
                relevant_payload = filter_event_payload(repo["eventFilter"], payload)
                if not relevant_payload:
                    continue

            template, error = loadTemplate(event, config["TEMPLATES_DIR"], '/mls/' + ml + '/', formatedRepoName)
            if not template:
                errors.append(error)
                continue
            body, subject = mailFromTemplate(template, payload)
            frum = repo.get("email", {}).get("from", config["EMAIL_FROM"])
            msgid = "<%s-%s-%s-%s>" % (event, event_id(event, payload),
                                       event_timestamp(event, payload), frum)
            target = "pull_request" if payload.has_key("pull_request") or payload.get("issue", {}).has_key("pull_request") else "issue"
            (ref_event, ref_id) = refevent(event, payload, target, config.get("GH_OAUTH_TOKEN", False))
            inreplyto = None
            if ref_event and ref_id:
                inreplyto = "<%s-%s-%s-%s>" % (ref_event, ref_id,
                                            event_timestamp(ref_event, payload),
                                            frum)

            too = repo.get("email", {}).get("to").split(",")
            headers = {}
            frum_name = ""

            if config.get("GH_OAUTH_TOKEN", False):
                headers['Authorization']="token %s" % (config["GH_OAUTH_TOKEN"])
                frum_name = requests.get(payload['sender']['url'],
                                     headers=headers
                                     ).json()['name']
                if frum_name == None:
                    frum_name = payload['sender']['login']
                frum_name = '%s via GitHub' % (frum_name)
            sentMail.append(sendMail(config["SMTP_HOST"], body, frum, frum_name, too, subject, msgid, inreplyto))
    return reportSentMail(sentMail, errors)

def reportSentMail(sentMail, errors):
    if sentMail:
        output = "Content-Type: application/json\n\n"
        output += json.dumps({'sent': sentMail, 'errors': errors})
        return output
    elif (len(errors)):
        output = "Status: 500 Error processing the request\n"
        output += "Content-Type: application/json\n\n"
        output += json.dumps({'errors': errors})
        return output
    else:
        output = "Content-Type: application/json\n\n"
        output += '"nothing done"'
        return output


def loadTemplate(name, rootpath, specificpath, optionalpath = ""):
    error = None
    template = None
    try:
        template = io.open(rootpath + specificpath + optionalpath + "/%s" % name).read()
    except IOError:
        try:
            template = io.open(rootpath + specificpath + "/%s" % name).read()
        except IOError:
            try:
                template = io.open(rootpath + "/generic/%s" % name).read()
            except IOError:
                error = {'msg': 'no template defined for event %s' % name}
    return template, error


def mailFromTemplate(template, payload):
    import pystache
    body = pystache.render(template, payload)
    subject, dummy, body = body.partition('\n')
    return body, subject

def sendMail(smtp, body, from_addr, from_name, to_addr, subject, msgid=None, inreplyto=None):
    s = smtplib.SMTP(smtp)
    msg = MIMEText(body, _charset="utf-8")
    readable_from = email.header.Header(charset='utf8', header_name='From')
    readable_from.append(from_name)
    readable_from.append('<%s>' % (from_addr), charset='us-ascii')
    msg.set_param('format', 'flowed')
    msg['From'] = readable_from
    msg['To'] = ",".join(to_addr)
    msg['Subject'] = Header(subject, 'utf-8')
    if msgid:
        msg['Message-ID'] = msgid
    if inreplyto:
        msg['In-Reply-To'] = inreplyto

    # from http://wordeology.com/computer/how-to-send-good-unicode-email-with-python.html
    m = StringIO()
    g = Generator(m, False)
    g.flatten(msg)
    s.sendmail(from_addr, to_addr, m.getvalue())
    sentMail = {"to":to_addr, "subject": subject}

    s.quit()
    return sentMail

if __name__ == "__main__":
    config = json.loads(io.open('instance/config.json').read())
    if os.environ.has_key('SCRIPT_NAME'):
        print serveRequest(config, sys.stdin.read())
    else:
        period = sys.argv[1] if len(sys.argv) > 1 else None
        sendDigest(config, period)
