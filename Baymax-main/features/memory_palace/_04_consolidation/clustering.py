"""Label-blind average-link clustering of existing local memory vectors."""
import hashlib
import json
import math

def average_link_clusters(similarities, threshold=0.40):
    """Label-blind average-link cosine clustering; no fixed cluster count."""
    groups = [[i] for i in range(len(similarities))]
    while len(groups) > 1:
        choices = [(sum(float(similarities[a][b]) for a in left for b in right) / (len(left)*len(right)), i, j)
                   for i,left in enumerate(groups) for j,right in enumerate(groups) if j > i]
        score, i, j = max(choices)
        if score < threshold:
            break
        groups[i] += groups.pop(j)
    return groups


def hierarchy_clusters(tree, detail=0):
    """Choose a display cut; automatic mode follows the largest observed score gap."""
    leaves, merges = tree['leaves'], tree['merges']
    count = len(leaves)
    if not merges:
        return []
    if detail:
        take = count - (1 + round((count-1)*(detail-1)/99))
    elif merges[0]['score'] <= 0:
        take = 0
    else:
        gaps = [a['score']-b['score'] for a,b in zip(merges,merges[1:])]
        take = gaps.index(max(gaps))+1 if gaps and max(gaps)>0 else len(merges)
    active = {i:[leaf['id']] for i,leaf in enumerate(leaves)}
    for i,merge in enumerate(merges[:take]):
        active[count+i] = active.pop(merge['left']) + active.pop(merge['right'])
    return [{**merges[index-count], 'scope':tree['scope'], 'members':members,
             'method':'average-link hierarchy; adaptive display cut',
             'status':'Display grouping only; weighted associations and sources remain separate'}
            for index,members in active.items() if index>=count]


def memory_hierarchies(sources):
    """Preserve every merge level; a display cut never changes stored memory ownership."""
    scopes = {}
    for source in sources:
        vector = source.get('vector')
        if (not source.get('scope') or source['kind'] == 'conversation' or not vector
            or not all(math.isfinite(x) for x in vector) or not any(vector)):
            continue
        scopes.setdefault((source['scope'], len(vector)), []).append(source)
    trees = []
    for (scope, _), rows in sorted(scopes.items()):
        rows = sorted(rows, key=lambda row:row['id'])
        normalized = []
        for row in rows:
            norm = math.sqrt(sum(y*y for y in row['vector']))
            normalized.append([x/norm for x in row['vector']])
        matrix = [[sum(x*y for x,y in zip(a,b)) for b in normalized] for a in normalized]
        active = {i:[i] for i in range(len(rows))}
        merges = []
        while len(active)>1:
            choices = [(sum(matrix[a][b] for a in left for b in right)/(len(left)*len(right)),i,j)
                       for i,left in active.items() for j,right in active.items() if j>i]
            score, left, right = max(choices)
            members = active.pop(left)+active.pop(right)
            medoid = max(members,key=lambda i:sum(matrix[i][j] for j in members if j!=i))
            cid = hashlib.sha256(json.dumps([scope,sorted(rows[i]['id'] for i in members)]).encode()).hexdigest()[:12]
            merges.append({'id':cid,'left':left,'right':right,'score':round(score,6),
                           'representative':rows[medoid]['id'],'label':rows[medoid]['note']})
            active[len(rows)+len(merges)-1] = members
        trees.append({'scope':scope,'leaves':[{'id':r['id'],'label':r['note']} for r in rows],'merges':merges})
    return trees


def memory_clusters(sources):
    return [cluster for tree in memory_hierarchies(sources) for cluster in hierarchy_clusters(tree)]
