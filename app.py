from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session
from openai import OpenAI
from dotenv import load_dotenv
import os
import requests
import re
from bs4 import BeautifulSoup
import json
import pandas as pd
from io import BytesIO
import time
import xml.etree.ElementTree as ET

load_dotenv()
app = Flask(__name__)
app.secret_key = "your_secret_key"  # أضف هذا السطر في الأعلى بعد app = Flask(__name__)
JSON_FILE = "test_cases.json"

# دالة لتحويل نص الخطوات إلى XML للتست كيس في Azure DevOps
def format_steps_xml(steps, expected_result_text=None):
    # إذا كانت steps قائمة من dicts (step/expected)
    if isinstance(steps, list) and isinstance(steps[0], dict):
        xml = '<?xml version="1.0" encoding="utf-8"?>'
        xml += f'<steps id="0" last="{len(steps)}">'
        for i, s in enumerate(steps, start=1):
            step_text = s.get("step", "")
            expected = s.get("expected", expected_result_text or "")
            xml += f'<step id="{i}">'
            xml += f'<parameterizedString isformatted="true"><![CDATA[{step_text}]]></parameterizedString>'
            xml += f'<parameterizedString isformatted="true"><![CDATA[{expected}]]></parameterizedString>'
            xml += '<executionStatus>NotExecuted</executionStatus>'
            xml += f'<expectedResult><![CDATA[{expected}]]></expectedResult>'
            xml += '</step>'
        xml += '</steps>'
        return xml
    # إذا كانت steps نص فقط
    return steps

# حفظ التست كيسات في ملف JSON
def save_test_cases(test_cases):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(test_cases, f, ensure_ascii=False, indent=4)

# تحميل التست كيسات من JSON
def load_test_cases():
    if not os.path.exists(JSON_FILE):
        return []
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# تنظيف HTML باستخدام BeautifulSoup
def clean_html(raw_html):
    soup = BeautifulSoup(raw_html, "html.parser")
    return soup.get_text()

# جلب تفاصيل الـ User Story من Azure DevOps
def get_user_story_details(story_id, project=None):
    org_url = os.getenv("AZURE_ORG_URL")
    pat = os.getenv("AZURE_PAT")
    
    project_part = f"/{project}" if project else ""
    url = f"{org_url}{project_part}/_apis/wit/workitems/{story_id}?$expand=relations&api-version=6.0"
    response = requests.get(url, auth=("", pat))
    
    if response.status_code != 200:
        return {
            "id": story_id,
            "title": "Error",
            "description": f"Could not fetch user story. Status code: {response.status_code}",
            "acceptance": "",
            "parent_title": "No Parent",
            "parent_type": ""
        }
    
    data = response.json()
    fields = data.get("fields", {})
    relations = data.get("relations", [])
    
    title = fields.get("System.Title", "No Title")
    description = clean_html(fields.get("System.Description", "No Description"))
    acceptance_criteria = clean_html(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "No Acceptance Criteria"))
    
    parent_id = None
    for relation in relations:
        if relation.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            parent_url = relation.get("url", "")
            parent_id = parent_url.split("/")[-1]
            break
    
    parent_title = "No Parent"
    parent_type = ""
    if parent_id:
        parent_url = f"{org_url}/{project}/_apis/wit/workitems/{parent_id}?api-version=6.0"
        parent_response = requests.get(parent_url, auth=("", pat))
        if parent_response.status_code == 200:
            parent_data = parent_response.json()
            parent_title = parent_data.get("fields", {}).get("System.Title", "Unknown Title")
            parent_type = parent_data.get("fields", {}).get("System.WorkItemType", "Unknown Type")
    
    return {
        "id": story_id,
        "title": title,
        "description": description,
        "acceptance": acceptance_criteria,
        "parent_title": parent_title,
        "parent_type": parent_type,
    }

# تحسين استخراج الـ User Story ID من الرابط أو الإدخال المباشر
def extract_story_id(input_str):
    if input_str.isdigit():
        return input_str
    match = re.search(r"/_workitems/edit/(\d+)", input_str)
    if match:
        return match.group(1)
    match = re.search(r"/workitems/(\d+)", input_str)
    if match:
        return match.group(1)
    return None

# إنشاء التست كيس الأساسي (بدون بيانات الخطوات) باستخدام ChatGPT
def generate_test_cases_with_openai(description, acceptance):
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    prompt = f"""
    User Story (in English):
    {description}

    Acceptance Criteria (in English):
    {acceptance}

    Write professional test cases in JSON format, where each test case has steps as a list of objects, each with 'step' and 'expected', and all content is in English:
    [
      {{
        "id": 1,
        "title": "Test Case Title",
        "steps": [
          {{ "step": "Open the page", "expected": "Page opens successfully" }},
          {{ "step": "Enter data", "expected": "Data is accepted" }}
        ],
        "expected_result": "Expected result for the test case"
      }}
    ]
    Only use English language for all fields and steps.
    """
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a QA engineer writing professional test cases in English only."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=1500
    )
    content = response.choices[0].message.content
    try:
        json_start = content.index('[')
        json_end = content.rindex(']') + 1
        test_cases = json.loads(content[json_start:json_end])
    except Exception:
        test_cases = [{
            "id": 1,
            "title": "Generated Test Case",
            "steps": [{"step": description, "expected": acceptance}],
            "expected_result": acceptance
        }]
    return test_cases

# خطوة 1: إنشاء التست كيس الأساسي (بدون خطوات)
def create_test_case_initial(story_id, title, expected_result):
    org_url = os.getenv("AZURE_ORG_URL")
    project = os.getenv("AZURE_PROJECT")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project}/_apis/wit/workitems/$Test%20Case?api-version=6.0"
    headers = {"Content-Type": "application/json-patch+json"}
    body = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.Description", "value": expected_result},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": 2},
        {"op": "add", "path": "/fields/System.Tags", "value": "Auto Created"}
    ]
    response = requests.patch(url, headers=headers, auth=("", pat), json=body)
    if response.status_code in (200, 201):
        return response.json()["id"]
    else:
        print("Error creating test case:", response.status_code, response.text)
        return None

# خطوة 2: تحديث التست كيس بإضافة الخطوات (Steps)
def update_test_case_steps(test_case_id, steps, expected_result=None):
    org_url = os.getenv("AZURE_ORG_URL")
    project = os.getenv("AZURE_PROJECT")
    pat = os.getenv("AZURE_PAT")
    headers = {"Content-Type": "application/json-patch+json"}
    Action_xml = format_steps_xml(steps, expected_result)
    url = f"{org_url}/{project}/_apis/wit/workitems/{test_case_id}?api-version=6.0"
    body = [
        {
            "op": "add",
            "path": "/fields/Microsoft.VSTS.TCM.Steps",
            "value": Action_xml
        }
    ]
    response = requests.patch(url, headers=headers, auth=("", pat), json=body)
    if response.status_code == 400 and "already exists" in response.text:
        body[0]["op"] = "replace"
        response = requests.patch(url, headers=headers, auth=("", pat), json=body)
    if response.status_code not in (200, 201):
        print("Error updating test steps:", response.status_code, response.text)
    return response.status_code

def link_test_case_to_user_story(story_id, test_case_id):
    org_url = os.getenv("AZURE_ORG_URL")
    project = os.getenv("AZURE_PROJECT")
    pat = os.getenv("AZURE_PAT")
    headers = {"Content-Type": "application/json-patch+json"}
    url = f"{org_url}/{project}/_apis/wit/workitems/{story_id}?api-version=6.0"
    body = [{
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "Microsoft.VSTS.Common.TestedBy-Forward",
            "url": f"{org_url}/_apis/wit/workitems/{test_case_id}"
        }
    }]
    response = requests.patch(url, headers=headers, auth=("", pat), json=body)
    if response.status_code not in (200, 201):
        print("Error linking test case to user story:", response.status_code, response.text)
    return response.status_code

# ============================================
# المسارات الرئيسية للتطبيق
# ============================================

@app.route("/", methods=["GET", "POST"])
def index():
    story_data, error, story_id = None, None, ""
    epics_list, features_list, user_stories_list = [], [], []
    epic_data, feature_data = None, None  # تأكد من تعريف المتغيرات
    work_item_type = None  # تعريف المتغير work_item_type

    if request.method == "POST":
        input_value = request.form.get("story_id", "").strip()
        story_id = extract_story_id(input_value)
        if not story_id:
            error = "من فضلك أدخل رقم أو رابط صحيح."
        else:
            project = os.getenv("AZURE_PROJECT", "اسم_مشروعك")
            work_item_type = get_work_item_type(story_id, project)
            if work_item_type == "Epic":
                epic_data = get_user_story_details(story_id, project)
                features_list = get_child_work_items(story_id, project, "Feature")
            elif work_item_type == "Feature":
                feature_data = get_user_story_details(story_id, project)
                user_stories_list = get_child_work_items(story_id, project, "Product Backlog Item")
            elif work_item_type == "Product Backlog Item":
                story_data = get_user_story_details(story_id, project)
                if "Could not fetch" in story_data["description"]:
                    error = f"لم يتم إيجاد الـ Product Backlog Item برقم {story_id}."
                else:
                    feature_data = get_parent_work_item(story_id, project, "Feature")
            else:
                error = f"لم يتم التعرف على نوع العنصر برقم {story_id}."

    history = load_test_cases_history()
    print("History Data:", history)  # Debugging

    return render_template(
        "index.html",
        story_id=story_id,
        story=story_data,
        epic_data=epic_data if work_item_type == "Epic" else None,
        feature_data=feature_data if work_item_type in ["Feature", "Product Backlog Item"] else None,
        epics_list=epics_list,
        features_list=features_list,
        user_stories_list=user_stories_list,
        error=error,
        lang=session.get("lang", "ar"),
        history=history,
    )

@app.route("/generate", methods=["POST"])
def generate():
    story_id = request.form.get("story_id")
    story = get_user_story_details(story_id)
    
    if story and "Could not fetch" not in story["description"]:
        if story["description"] and story["acceptance"]:
            test_cases = generate_test_cases_with_openai(story["description"], story["acceptance"])
        else:
            test_cases = []
            print("Error: Description or acceptance criteria is empty or invalid.")
        
        for tc in test_cases:
            print(f"Test Case: {tc}")  # Debugging
            tc["story_id"] = story_id
            if "expected_result" not in tc:
                tc["expected_result"] = "No expected result provided"
            
            test_case_id = create_test_case_initial(story_id, tc["title"], tc["expected_result"])
            if test_case_id:
                update_test_case_steps(test_case_id, tc["steps"], tc["expected_result"])
                link_test_case_to_user_story(story_id, test_case_id)
                time.sleep(1)
        
        save_test_cases(test_cases)

        # تحديث الهيستوري
        story_title = story.get("title", "")
        history = load_test_cases_history()
        history.append({
            "story_id": story_id,
            "story_title": story_title,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            "test_cases": [
                dict(tc, generated=True) for tc in test_cases
            ]
        })
        save_test_cases_history(history)

        return jsonify({"status": "success", "test_cases": test_cases})
    else:
        return jsonify({"status": "error", "message": "Error fetching user story or missing description/acceptance."})

@app.route("/update_test_case", methods=["POST"])
def update_test_case():
    data = request.json
    test_cases = load_test_cases()
    for i, tc in enumerate(test_cases):
        if tc["id"] == data["id"]:
            test_cases[i].update({
                "title": data["title"],
                "steps": data["steps"],
                "expected_result": data["expected_result"]
            })
            break
    save_test_cases(test_cases)
    return jsonify({"status": "success"})

@app.route("/delete_test_case/<int:tc_id>", methods=["POST"])
def delete_test_case(tc_id):
    test_cases = load_test_cases()
    test_cases = [tc for tc in test_cases if tc["id"] != tc_id]
    save_test_cases(test_cases)
    return jsonify({"status": "success"})

@app.route("/push_to_azure", methods=["POST"])
def push_to_azure():
    story_id = request.json.get("story_id")
    test_cases = load_test_cases()
    updated = False

    for tc in test_cases:
        if tc.get("story_id") == story_id:
            if tc.get("id"):  # إذا كان له ID (Azure ID)
                # تحقق من وجود تغييرات
                original_tc = fetch_test_case_from_azure(tc["id"])
                if original_tc and original_tc == tc:
                    return jsonify({"status": "error", "message": "No changes to push."})
                # تحديث التست كيس على Azure
                update_test_case_on_azure(tc)
                updated = True

    if updated:
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "error", "message": "No test cases were updated."})

@app.route("/export_excel", methods=["GET"])
def export_excel():
    test_cases = load_test_cases()
    if not test_cases:
        return redirect(url_for("index"))
    df = pd.DataFrame(test_cases)
    df.drop(columns=["story_id"], errors="ignore", inplace=True)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="Test Cases")
    output.seek(0)
    return send_file(output, download_name="test_cases.xlsx", as_attachment=True)

def save_test_cases_history(history):
    with open("test_cases_history.json", "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

def load_test_cases_history():
    if not os.path.exists("test_cases_history.json"):
        return []
    with open("test_cases_history.json", "r", encoding="utf-8") as f:
        return json.load(f)

@app.route("/switch_language", methods=["GET"])
def switch_language():
    lang = session.get("lang", "ar")
    session["lang"] = "en" if lang == "ar" else "ar"
    return redirect(url_for("index"))

def get_work_item_type(work_item_id, project):
    org_url = os.getenv("AZURE_ORG_URL")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project}/_apis/wit/workitems/{work_item_id}?api-version=6.0"
    response = requests.get(url, auth=("", pat))
    if response.status_code != 200:
        return None
    data = response.json()
    return data.get("fields", {}).get("System.WorkItemType", "")

def get_child_user_stories(parent_id, project):
    org_url = os.getenv("AZURE_ORG_URL")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project}/_apis/wit/workitems/{parent_id}?$expand=relations&api-version=6.0"
    response = requests.get(url, auth=("", pat))
    if response.status_code != 200:
        return []
    data = response.json()
    relations = data.get("relations", [])
    user_stories = []
    for relation in relations:
        if relation.get("rel") == "System.LinkTypes.Hierarchy-Forward":
            child_url = relation.get("url", "")
            child_id = child_url.split("/")[-1]
            # تحقق أن النوع User Story
            child_type = get_work_item_type(child_id, project)
            if child_type == "User Story":
                # جلب العنوان
                child_data = get_user_story_details(child_id, project)
                user_stories.append(child_data)
    return user_stories

def get_azure_projects():
    org_url = os.getenv("AZURE_ORG_URL")
    pat = os.getenv("AZURE_PAT")
    org_url = org_url.rstrip("/")
    url = f"{org_url}/_apis/projects?api-version=6.0"
    try:
        response = requests.get(url, auth=("", pat), timeout=10)
        print("Azure Projects Response:", response.status_code, response.text)  # Debugging
        if response.status_code != 200:
            print(f"Error fetching projects: {response.status_code}, {response.text}")
            return []
        data = response.json()
        print("Projects Data:", data)  # Debugging
        return [{"id": p["id"], "name": p["name"]} for p in data.get("value", [])]
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")
        return []

@app.route("/delete_all_test_cases", methods=["POST"])
def delete_all_test_cases():
    save_test_cases([])
    return jsonify({"status": "success"})

@app.route("/fetch_azure_test_cases", methods=["POST"])
def fetch_azure_test_cases():
    if request.content_type != "application/json":
        return jsonify({"status": "error", "message": "Unsupported Media Type"}), 415

    story_id = request.json.get("story_id")
    if not story_id:
        return jsonify({"status": "error", "message": "Story ID is required."}), 400

    org_url = os.getenv("AZURE_ORG_URL")
    project = os.getenv("AZURE_PROJECT")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project}/_apis/wit/workitems/{story_id}?$expand=relations&api-version=6.0"
    response = requests.get(url, auth=("", pat))

    if response.status_code != 200:
        return jsonify({"status": "error", "message": "Failed to fetch test cases from Azure."}), 500

    data = response.json()
    relations = data.get("relations", [])
    test_case_ids = [
        rel["url"].split("/")[-1]
        for rel in relations
        if rel.get("rel") == "Microsoft.VSTS.Common.TestedBy-Forward"
    ]
    test_cases = []
    for tc_id in test_case_ids:
        tc_url = f"{org_url}/{project}/_apis/wit/workitems/{tc_id}?api-version=6.0"
        tc_resp = requests.get(tc_url, auth=("", pat))
        if tc_resp.status_code == 200:
            tc_data = tc_resp.json()
            fields = tc_data.get("fields", {})
            steps_xml = fields.get("Microsoft.VSTS.TCM.Steps", "")
            steps = parse_azure_steps_xml(steps_xml)
            test_cases.append({
                "id": tc_id,
                "title": fields.get("System.Title", ""),
                "steps": steps,
                "expected_result": fields.get("System.Description", ""),
                "story_id": story_id
            })

    save_test_cases(test_cases)
    return jsonify({"status": "success", "test_cases": test_cases})

def parse_azure_steps_xml(xml_str):
    steps = []
    if not xml_str:
        return steps
    try:
        root = ET.fromstring(xml_str)
        for step in root.findall(".//step"):
            step_text = step.find("parameterizedString")
            expected = step.find("expectedResult")
            steps.append({
                "step": step_text.text if step_text is not None else "",
                "expected": expected.text if expected is not None else ""
            })
    except Exception as e:
        # إذا كان XML غير صالح، أعده كنص واحد
        steps = [{"step": xml_str, "expected": ""}]
    return steps

def update_test_case_on_azure(tc):
    org_url = os.getenv("AZURE_ORG_URL")
    project = os.getenv("AZURE_PROJECT")
    pat = os.getenv("AZURE_PAT")
    headers = {"Content-Type": "application/json-patch+json"}
    url = f"{org_url}/{project}/_apis/wit/workitems/{tc['id']}?api-version=6.0"
    body = [
        {"op": "add", "path": "/fields/System.Title", "value": tc["title"]},
        {"op": "add", "path": "/fields/System.Description", "value": tc["expected_result"]},
    ]
    response = requests.patch(url, headers=headers, auth=("", pat), json=body)
    if response.status_code not in (200, 201):
        print("Error updating test case:", response.status_code, response.text)
    # تحديث الخطوات أيضاً
    update_test_case_steps(tc["id"], tc["steps"], tc["expected_result"])

@app.route("/projects", methods=["GET"])
def get_projects():
    projects = get_azure_projects()
    return render_template("projects.html", projects=projects)

@app.route("/epics/<project_id>", methods=["GET"])
def get_epics(project_id):
    epics = get_work_items_by_type(project_id, "Epic")
    return render_template("epics.html", epics=epics, project_id=project_id)

@app.route("/features/<project_id>/<epic_id>", methods=["GET"])
def get_features(project_id, epic_id):
    features = get_child_work_items(epic_id, project_id, "Feature")
    print(f"Features Data: {features}")  # Debugging
    return render_template("features.html", features=features, project_id=project_id, epic_id=epic_id)

@app.route("/user_stories/<project_id>/<feature_id>", methods=["GET"])
def get_user_stories(project_id, feature_id):
    user_stories = get_child_work_items(feature_id, project_id, "User Story")
    return render_template("user_stories.html", user_stories=user_stories, project_id=project_id, feature_id=feature_id)

def get_work_items_by_type(project_id, work_item_type):
    org_url = os.getenv("AZURE_ORG_URL").rstrip("/")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project_id}/_apis/wit/wiql?api-version=6.0"
    query = f"""
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.WorkItemType] = '{work_item_type}'
    """
    print(f"WIQL Query: {query}")  # Debugging
    response = requests.post(url, auth=("", pat), json={"query": query})
    
    if response.status_code != 200:
        print(f"Error fetching work items: {response.status_code}, {response.text}")  # Debugging
        return []
    
    data = response.json()
    print(f"Work Items Data: {data}")  # Debugging
    work_items = data.get("workItems", [])
    detailed_items = []
    
    for item in work_items:
        item_id = item.get("id")
        if item_id:
            item_url = f"{org_url}/{project_id}/_apis/wit/workitems/{item_id}?api-version=6.0"
            item_response = requests.get(item_url, auth=("", pat))
            if item_response.status_code == 200:
                item_data = item_response.json()
                title = item_data.get("fields", {}).get("System.Title", "Unknown Title")
                detailed_items.append({"id": item_id, "title": title})
            else:
                print(f"Error fetching item details: {item_response.status_code}, {item_response.text}")
                detailed_items.append({"id": item_id, "title": "Unknown Title"})
    
    return detailed_items

def get_child_work_items(parent_id, project_id, child_type):
    org_url = os.getenv("AZURE_ORG_URL")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project_id}/_apis/wit/workitems/{parent_id}?$expand=relations&api-version=6.0"
    response = requests.get(url, auth=("", pat))
    
    if response.status_code != 200:
        print(f"Error fetching child work items: {response.status_code}, {response.text}")  # Debugging
        return []
    
    data = response.json()
    relations = data.get("relations", [])
    
    child_items = []
    for rel in relations:
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward":
            child_url = rel.get("url", "")
            child_id = child_url.split("/")[-1]
            
            # جلب بيانات العنصر الفرعي مباشرة
            item_url = f"{org_url}/{project_id}/_apis/wit/workitems/{child_id}?api-version=6.0"
            item_response = requests.get(item_url, auth=("", pat))
            if item_response.status_code == 200:
                item_data = item_response.json()
                item_type = item_data.get("fields", {}).get("System.WorkItemType", "")
                if item_type == child_type:  # تحقق من النوع
                    title = item_data.get("fields", {}).get("System.Title", "Unknown Title")
                    status = item_data.get("fields", {}).get("System.State", "Unknown Status")
                    child_items.append({"id": child_id, "title": title, "status": status})
            else:
                print(f"Error fetching child item details: {item_response.status_code}, {item_response.text}")
    return child_items

@app.route("/api/projects", methods=["GET"])
def api_get_projects():
    projects = get_azure_projects()
    print(f"Projects Data: {projects}")  # Debugging
    return jsonify(projects)

@app.route("/api/epics/<project_id>", methods=["GET"])
def api_get_epics(project_id):
    print(f"Fetching Epics for Project ID: {project_id}")  # Debugging
    epics = get_work_items_by_type(project_id, "Epic")
    print(f"Epics Data: {epics}")  # Debugging
    return jsonify(epics)

@app.route("/api/features/<project_id>/<epic_id>", methods=["GET"])
def api_get_features(project_id, epic_id):
    features = get_child_work_items(epic_id, project_id, "Feature")
    print(f"Features Data: {features}")  # Debugging
    return jsonify(features)

@app.route("/api/user_stories/<project_id>/<feature_id>", methods=["GET"])
def api_get_user_stories(project_id, feature_id):
    user_stories = get_child_work_items(feature_id, project_id, "Product Backlog Item")
    for story in user_stories:
        story_id = story["id"]
        story["test_cases"] = []  # جلب التست كيس من Azure فقط
        org_url = os.getenv("AZURE_ORG_URL")
        project = os.getenv("AZURE_PROJECT")
        pat = os.getenv("AZURE_PAT")
        url = f"{org_url}/{project}/_apis/wit/workitems/{story_id}?$expand=relations&api-version=6.0"
        response = requests.get(url, auth=("", pat))
        if response.status_code == 200:
            data = response.json()
            relations = data.get("relations", [])
            test_case_ids = [
                rel["url"].split("/")[-1]
                for rel in relations
                if rel.get("rel") == "Microsoft.VSTS.Common.TestedBy-Forward"
            ]
            for tc_id in test_case_ids:
                tc_url = f"{org_url}/{project}/_apis/wit/workitems/{tc_id}?api-version=6.0"
                tc_resp = requests.get(tc_url, auth=("", pat))
                if tc_resp.status_code == 200:
                    tc_data = tc_resp.json()
                    fields = tc_data.get("fields", {})
                    steps_xml = fields.get("Microsoft.VSTS.TCM.Steps", "")
                    steps = parse_azure_steps_xml(steps_xml)
                    story["test_cases"].append({
                        "id": tc_id,
                        "title": fields.get("System.Title", ""),
                        "steps": steps,
                        "expected_result": fields.get("System.Description", ""),
                    })
    return jsonify(user_stories)

@app.route("/api/user_story_details/<story_id>", methods=["GET"])
def api_get_user_story_details(story_id):
    story = get_user_story_details(story_id)
    test_cases = []  # جلب التست كيس من Azure فقط
    org_url = os.getenv("AZURE_ORG_URL")
    project = os.getenv("AZURE_PROJECT")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project}/_apis/wit/workitems/{story_id}?$expand=relations&api-version=6.0"
    response = requests.get(url, auth=("", pat))
    if response.status_code == 200:
        data = response.json()
        relations = data.get("relations", [])
        test_case_ids = [
            rel["url"].split("/")[-1]
            for rel in relations
            if rel.get("rel") == "Microsoft.VSTS.Common.TestedBy-Forward"
        ]
        for tc_id in test_case_ids:
            tc_url = f"{org_url}/{project}/_apis/wit/workitems/{tc_id}?api-version=6.0"
            tc_resp = requests.get(tc_url, auth=("", pat))
            if tc_resp.status_code == 200:
                tc_data = tc_resp.json()
                fields = tc_data.get("fields", {})
                steps_xml = fields.get("Microsoft.VSTS.TCM.Steps", "")
                steps = parse_azure_steps_xml(steps_xml)
                test_cases.append({
                    "id": tc_id,
                    "title": fields.get("System.Title", ""),
                    "steps": steps,
                    "expected_result": fields.get("System.Description", ""),
                    "story_id": story_id
                })
    story["test_cases"] = test_cases
    return jsonify(story)

def delete_test_case_on_azure(test_case_id):
    org_url = os.getenv("AZURE_ORG_URL")
    project = os.getenv("AZURE_PROJECT")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project}/_apis/wit/workitems/{test_case_id}?api-version=6.0"
    headers = {"Content-Type": "application/json-patch+json"}
    body = [{"op": "remove", "path": "/fields/System.Title"}]
    response = requests.patch(url, headers=headers, auth=("", pat), json=body)
    if response.status_code not in (200, 201):
        print("Error deleting test case:", response.status_code, response.text)

@app.route("/regenerate", methods=["POST"])
def regenerate():
    story_id = request.form.get("story_id")
    story = get_user_story_details(story_id)
    
    if story and "Could not fetch" not in story["description"]:
        if story["description"] and story["acceptance"]:
            # إنشاء التست كيس الجديدة باستخدام OpenAI
            test_cases = generate_test_cases_with_openai(story["description"], story["acceptance"])
        else:
            test_cases = []
            print("Error: Description or acceptance criteria is empty or invalid.")
        
        new_test_cases = []
        for tc in test_cases:
            print(f"Test Case: {tc}")  # Debugging
            tc["story_id"] = story_id
            if "expected_result" not in tc:
                tc["expected_result"] = "No expected result provided"
            
            test_case_id = create_test_case_initial(story_id, tc["title"], tc["expected_result"])
            if test_case_id:
                update_test_case_steps(test_case_id, tc["steps"], tc["expected_result"])
                link_test_case_to_user_story(story_id, test_case_id)
                new_test_cases.append(tc)
                time.sleep(1)
        
        # حفظ التست كيس الجديدة فقط
        save_test_cases(new_test_cases)

        # تحديث الهيستوري
        story_title = story.get("title", "")
        history = load_test_cases_history()
        history.append({
            "story_id": story_id,
            "story_title": story_title,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            "test_cases": [
                dict(tc, regenerated=True) for tc in new_test_cases
            ]
        })
        save_test_cases_history(history)

        return jsonify({"status": "success", "test_cases": new_test_cases})
    else:
        return jsonify({"status": "error", "message": "Error fetching user story or missing description/acceptance."})

def get_parent_work_item(work_item_id, project, parent_type):
    org_url = os.getenv("AZURE_ORG_URL")
    pat = os.getenv("AZURE_PAT")
    url = f"{org_url}/{project}/_apis/wit/workitems/{work_item_id}?$expand=relations&api-version=6.0"
    response = requests.get(url, auth=("", pat))
    if response.status_code != 200:
        return None
    data = response.json()
    relations = data.get("relations", [])
    for relation in relations:
        if relation.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            parent_url = relation.get("url", "")
            parent_id = parent_url.split("/")[-1]
            parent_data = get_user_story_details(parent_id, project)
            if parent_data.get("parent_type") == parent_type:
                return parent_data
    return None

if __name__ == "__main__":
    app.run(debug=True)