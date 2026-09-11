# H-1B Job Search MCP: usage guide

Use the **H-1B Job Search MCP** to explore historical H-1B sponsorship data
through a compatible AI client. The data is a research signal, not a promise
that an employer currently sponsors or is hiring.

## Quick start

Ask for the data you need in plain English. These are common requests.

### 1. Load data

```
"Load the latest H-1B data"
"Get me the 2024 Q4 H-1B data"
"Download fresh H-1B sponsor data"
```

Typical result:
```
✅ Successfully loaded the latest available H-1B disclosure period
📅 The response includes the exact fiscal year and quarter that were loaded
📊 Data cached for faster future searches
💡 Ready to search! Try: "Find software engineer jobs in California"
```

### 2. Find jobs

#### Basic Job Search
```
"Find software engineer jobs"
"Show me data scientist positions"
"I'm looking for product manager roles"
```

#### Location-Specific Search
```
"Find software engineer jobs in San Francisco"
"Show me tech jobs in Seattle, WA"
"I want data analyst positions in New York City"
```

#### Search by salary
```
"Find software engineer jobs paying over $150,000"
"Show me data scientist roles in California with minimum $120k salary"
"I need tech jobs in Austin paying at least $100,000"
```

#### Exclude staffing agencies
```
"Find direct hire software engineer positions (no agencies)"
"Show me companies directly hiring data scientists, skip the consultancies"
"I want product manager jobs but not from staffing companies"
```

### 3. Research companies

```
"Tell me about Google's H-1B sponsorships"
"How many H-1B visas does Microsoft sponsor?"
"What positions does Apple typically sponsor?"
"Show me Amazon's H-1B statistics"
```

Typical result:
```
📊 Google H-1B Statistics:
- Total Applications: 1,234
- Certified: 1,200 (97.2%)
- Top Positions: Software Engineer, Data Scientist, Product Manager
- Salary Range: $120,000 - $350,000
- Average Salary: $185,000
- Top Locations: Mountain View (CA), New York (NY), Seattle (WA)
```

### 4. Find top sponsors

```
"Who are the top H-1B sponsors?"
"Show me the top 20 companies sponsoring H-1B"
"Which companies sponsor the most H-1B visas (not agencies)?"
```

### 5. Export results

```
"Export software engineer jobs in California to a spreadsheet"
"Save my search results for data scientist jobs to CSV"
"Create an Excel file with all Python developer positions"
```

## Natural-language prompts

The tool accepts different phrasings for the same request.

### Search prompts

These prompts express the same goal:
- "I'm a software engineer looking for jobs in the Bay Area"
- "Help me find SWE positions in Silicon Valley"
- "software developer opportunities san francisco"
- "coding jobs near San Jose, California"
- "I want to work as a programmer in the Bay Area"

### Filter prompts

These prompts apply the same salary filter:
- "...paying over 150k"
- "...with salary above $150,000"
- "...minimum compensation 150000"
- "...at least 150K per year"

### Exclude-agency prompts

These prompts exclude staffing companies:
- "...no agencies"
- "...direct hire only"
- "...skip consultancies"
- "...not from staffing firms"
- "...avoid Infosys, TCS, Wipro"

## Example workflows

The figures in these examples are illustrative. Query the data to see the
current results for a company, role, or location.

### Example 1: Fresh Graduate Software Engineer

```
You: "I'm a new CS grad looking for entry-level software engineer jobs"

AI: Loading H-1B data...

You: "Show me junior developer positions in Texas"

AI: Found 145 companies sponsoring entry-level developers in Texas:
1. Dell Technologies - Austin, TX - $85,000-$95,000
2. Texas Instruments - Dallas, TX - $80,000-$90,000
3. Oracle - Austin, TX - $90,000-$100,000
...

You: "Tell me more about Dell's H-1B program"

AI: Dell Technologies H-1B Statistics:
- 89 H-1B applications
- 85 certified (95.5%)
- Top roles: Software Engineer, Systems Engineer, Data Analyst
- Average salary: $92,000
- Primary location: Austin, TX

You: "Export these Texas junior developer jobs to a file"

AI: ✅ Exported 145 results to 'h1b_texas_junior_dev.csv'
```

### Example 2: Experienced Data Scientist

```
You: "I'm a senior data scientist with 8 years experience, looking for high-paying roles"

AI: Loading H-1B data...

You: "Find data scientist positions paying over $180,000, no agencies"

AI: Found 67 companies offering $180k+ for data scientists:
1. Google - Mountain View, CA - $200,000-$350,000
2. Meta - Menlo Park, CA - $195,000-$320,000
3. Netflix - Los Gatos, CA - $250,000-$400,000
...

You: "Which of these companies sponsor the most data scientists?"

AI: Top data scientist sponsors (from your filtered list):
1. Google - 145 data scientist H-1Bs
2. Meta - 98 data scientist H-1Bs
3. Microsoft - 87 data scientist H-1Bs

You: "Save this list for me"

AI: ✅ Exported 67 high-paying data scientist positions to 'senior_data_scientist_180k.csv'
```

### Example 3: Specific City Job Hunt

```
You: "I want to move to Seattle for a tech job"

AI: Loading H-1B data...

You: "What tech companies in Seattle sponsor H-1B?"

AI: Major H-1B sponsors in Seattle:
1. Amazon - 2,341 applications
2. Microsoft - 1,876 applications
3. Expedia - 234 applications
4. Zillow - 156 applications
5. Tableau - 143 applications
...

You: "Show me software engineer jobs at these companies"

AI: Software Engineer positions in Seattle:
- Amazon: 453 positions, $120k-$185k avg
- Microsoft: 387 positions, $115k-$175k avg
- Expedia: 45 positions, $110k-$155k avg
...

You: "Which one has the best approval rate?"

AI: H-1B Approval Rates (Seattle tech companies):
1. Microsoft: 98.2% approval rate
2. Amazon: 96.5% approval rate
3. Tableau: 95.8% approval rate
```

## Practical search tips

### 1. Name the role
Instead of: "tech jobs"
Try: "backend engineer", "iOS developer", "machine learning engineer"

### 2. Use standard job titles
The database uses common titles like:
- Software Engineer (not "Coder")
- Data Scientist (not "Data Wizard")
- Product Manager (not "Product Owner")

### 3. Specify locations when possible
- Use state codes: "CA", "NY", "TX"
- Major cities also work: "San Francisco", "New York", "Austin"

### 4. Narrow the search in stages
Start broad, then narrow:
1. "Show me all data science jobs"
2. "Filter those for California"
3. "Now show only those paying over $150k"
4. "Remove staffing agencies"

### 5. Research before applying
Always check:
- Company's H-1B approval rate
- Average salaries for your role
- Number of similar positions sponsored
- Primary office locations

## Understanding results

### What the data shows

**For Job Searches:**
- Company name (actual employer)
- Job title (as filed with Department of Labor)
- Location (city and state)
- Wage information (when available)
- Contact information (when available)
- Company statistics calculated across all loaded positions for that employer

**For Company Statistics:**
- Total H-1B applications
- Number of certified applications
- Success rate percentage
- Top job titles sponsored
- Salary ranges and averages
- Primary locations

**For Top Sponsors:**
- Ranking by volume
- Total applications per company
- Average wages offered
- Primary state of operation

## Data details

### When new data appears
- New quarter data becomes available ~45 days after quarter end
- Q4 data (October-December) available in mid-February
- Q1 data (January-March) available in mid-May

### Understanding Sponsors
- **Direct Employers**: Companies hiring for their own teams
- **Staffing Agencies**: Companies that place you at client sites
- **Consultancies**: Mix of direct work and client placements

### Salary Expectations
Salaries in the database are:
- **Minimum wages** as per LCA requirements
- Actual offers may be higher
- Usually doesn't include bonuses/stock
- Regional variations apply

## Avoid these common search errors

### Broad or ambiguous prompts
```
"Show me all jobs" (too broad, will return too many results)
"Find work" (too vague)
"H1B" (need to specify what you want to know)
```

### More useful alternatives
```
"Find software engineer jobs in California"
"Show me data scientist positions paying over $120k"
"Which companies sponsor the most H-1B visas?"
```

## Troubleshooting

### "No data loaded"
**Solution:** First run: "Load the latest H-1B data"

### "No results found"
**Try:**
- Broader search terms: "engineer" instead of "senior backend engineer"
- Different locations: "CA" instead of specific city
- Remove filters: Try without salary requirements first

### "Too many results"
**Try:**
- Add location: "...in California"
- Add salary filter: "...paying over $100k"
- Exclude agencies: "...no staffing agencies"

## Advanced queries

### Combining Multiple Criteria
```
"Find machine learning engineer positions in California or Washington, 
paying over $150k, at companies that are not consultancies, 
and export the top 50 results"
```

### Competitive Analysis
```
"Compare H-1B sponsorships between Google, Microsoft, and Amazon 
for software engineer positions"
```

### Trend Analysis
```
"Show me how many data scientist H-1Bs were sponsored 
in Q1 vs Q2 vs Q3 vs Q4 of 2024"
```

## Quick reference

| What You Want | What to Say |
|--------------|-------------|
| Load data | "Load H-1B data" |
| Find jobs | "Find [job title] jobs" |
| Add location | "...in [city/state]" |
| Add salary | "...paying over $[amount]" |
| Skip agencies | "...no agencies" |
| Company info | "Tell me about [company] H-1B" |
| Top sponsors | "Top H-1B sponsors" |
| Export | "Export results to CSV" |

## A simple starting sequence

1. **Load the data**: "Load the latest H-1B data"
2. **Search for your role**: "Find [your job title] positions"
3. **Filter by location**: "...in [your preferred city]"
4. **Research companies**: "Tell me about [company name]"
5. **Export results**: "Save these results to a file"
6. **Use the data carefully**: Treat historical filing patterns as one input to your research.

More specific requests return more focused results. Verify current sponsorship
policies and open roles with each employer before you apply.
